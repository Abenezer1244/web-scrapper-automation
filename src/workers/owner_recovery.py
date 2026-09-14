"""Background recovery of King tax owner names a job could not look up.

WHY THIS EXISTS
---------------
King tax leads get their owner name from eRealProperty, one page per parcel,
under a shared source lease and a wall-clock budget. A job that loses the lease,
runs out of budget or meets a throttle leaves leads unnamed and (since #298) marks
each one `owner_lookup_deferred = true` with the reason. Job b2f2ecd5 delivered 840
King tax leads with 0 owner names; without this sweep they would stay that way
until someone re-ran the scraper.

WHAT THIS IS NOT
----------------
An OWNER-NAME BACKFILL and nothing else, on the same boundary as the mailing
sweep in mailing_recovery.py:

  * It never creates a Job and never touches quota, billing, `reserved_count` or
    `records_used`.
  * It never enqueues a skip trace (Tracerfy keys off the property address, which
    this never changes) and never touches `skip_trace_status`, phone or email.
  * It only fills a `party_name` that is still blank, on a row that is still
    deferred and still delivered, on a terminal job. Each condition is re-checked
    in the UPDATE itself, so a row a re-run named, or the plan cap excluded, while
    the lookup was in flight is left alone.

WHICH LEADS
-----------
Only DELIVERED leads: non-duplicate rows the plan cap did not mark over quota.
Over-quota rows and duplicates are never shown to the customer, and every
eRealProperty request costs goodwill with a source that has IP-blocked us twice.
An over-quota lead delivered later by another job is named by that job's own
enrichment. Largest balance first, the order the tax plan cap delivers in.

KING RATE LIMITS
----------------
The lookup runs through `batch_extract_king_owners`, which holds the shared
SourceAdmission lease. Every King eRealProperty pass (job phase 1, job owner pass,
mailing sweep, this sweep) takes the same lease, so only one of them talks to King
at a time and this one paces 1 request per second inside it. The source-health gate
and the owner breaker stop it on a throttle, and `OWNER_RECOVERY_ENABLED` stops it
before any request.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime

from sqlalchemy import text as sa_text

from src.config import settings
from src.utils.logger import setup_logger
from src.workers.tasks_helpers.enrich import (
    OWNER_DEFERRED_KEY,
    OWNER_DEFERRED_REASON_KEY,
    OWNER_NOT_ON_RECORD,
    OWNER_OUTCOME_KEY,
)

_logger = setup_logger("worker.owner_recovery")

ATTEMPTS_KEY = "owner_recovery_attempts"
LAST_AT_KEY = "owner_recovery_last_at"
RECOVERY_OUTCOME_KEY = "owner_recovery_outcome"

# A transient failure is retried this many times across ticks, then the lead is
# settled as `gave_up` so a permanently failing parcel cannot hold its place.
_MAX_ATTEMPTS = 5

# Parcels per tick. At `_PACE_S` that is about two minutes of requests, inside
# `_TICK_BUDGET_S`, and small against the 15-minute schedule.
_BATCH_PARCELS = 120
_PACE_S = 1.0
_TICK_BUDGET_S = 300.0

_LOCK_KEY = "bl:owner_recovery:lock"
_LOCK_TTL_S = 1200

# Delivered King tax leads still waiting for an owner. The same predicate guards
# every write (`_ELIGIBLE_ROW` below), so selection and write cannot disagree.
_ELIGIBLE_ROW = """
      r.is_duplicate = false
  AND r.enrichment_data->>'delivery_excluded_reason' IS NULL
  AND coalesce(r.enrichment_data->>'owner_lookup_deferred', '') = 'true'
  AND (r.party_name IS NULL OR btrim(r.party_name) = '')
  AND btrim(r.parcel_id) ~ '^[0-9]{10}$'
  AND coalesce((r.enrichment_data->>'owner_recovery_attempts')::int, 0) < :max_attempts
"""

# Pick PARCELS, not rows (two leads on one parcel share one lookup): fewest
# attempts first, then the longest since last tried, then the largest balance.
_CANDIDATE_PARCELS_SQL = f"""
    SELECT btrim(r.parcel_id) AS parcel_id,
           min(coalesce((r.enrichment_data->>'owner_recovery_attempts')::int, 0)) AS attempts,
           min(coalesce(r.enrichment_data->>'owner_recovery_last_at', '')) AS last_at,
           max(r.delinquent_amount) AS amount
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE j.status = 'done'
      AND lower(sc.county) = 'king' AND upper(sc.state) = 'WA'
      AND sc.record_type = 'tax_delinquent'
      AND {_ELIGIBLE_ROW}
    GROUP BY btrim(r.parcel_id)
    ORDER BY attempts ASC, last_at ASC, amount DESC NULLS LAST, parcel_id ASC
    LIMIT :batch
"""  # noqa: S608 -- splices only the _ELIGIBLE_ROW constant; every value is bound

_CANDIDATE_ROWS_SQL = f"""
    SELECT r.id, r.user_id, btrim(r.parcel_id) AS parcel_id, r.enrichment_data
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE j.status = 'done'
      AND lower(sc.county) = 'king' AND upper(sc.state) = 'WA'
      AND sc.record_type = 'tax_delinquent'
      AND {_ELIGIBLE_ROW}
      AND btrim(r.parcel_id) = ANY(:parcels)
    ORDER BY r.id
"""  # noqa: S608 -- splices only the _ELIGIBLE_ROW constant; every value is bound

# The write re-applies the eligibility predicate against the row AND its job as they
# are NOW, so neither a row nor a job that changed during the lookup is written.
_WRITE_SQL = f"""
    UPDATE results r SET
      party_name = COALESCE(CAST(:owner AS varchar), r.party_name),
      enrichment_data = (
        ((CASE WHEN jsonb_typeof(r.enrichment_data::jsonb) = 'object'
               THEN r.enrichment_data::jsonb ELSE '{{}}'::jsonb END)
         - CAST(:drop_key AS text))
        || CAST(:payload AS jsonb))::json
    WHERE r.id = :rid AND r.user_id = :uid AND btrim(r.parcel_id) = :pid
      AND {_ELIGIBLE_ROW}
      AND EXISTS (
        SELECT 1 FROM jobs j JOIN scraper_configs sc ON sc.id = j.scraper_config_id
        WHERE j.id = r.job_id AND j.status = 'done'
          AND lower(sc.county) = 'king' AND upper(sc.state) = 'WA'
          AND sc.record_type = 'tax_delinquent')
"""  # noqa: S608 -- splices only the _ELIGIBLE_ROW constant; every value is bound


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _acquire_lock():
    """Redis SET NX single-flight. None = another tick holds it; False = no Redis, run."""
    try:
        import redis as sync_redis

        client = sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
        return client if client.set(_LOCK_KEY, _now_iso(), nx=True, ex=_LOCK_TTL_S) else None
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Owner recovery: lock unavailable (%s), running unlocked", str(exc)[:120])
        return False


def _release_lock(client) -> None:
    if not client:
        return
    try:
        client.delete(_LOCK_KEY)
    except Exception:  # noqa: BLE001, S110 -- the TTL releases it anyway
        pass


def recover_deferred_king_owners() -> dict:
    """One bounded tick. Returns a stats dict (also read by the tests)."""
    stats = {"parcels": 0, "rows": 0, "found": 0, "not_on_record": 0, "parcel_mismatch": 0,
             "transient": 0, "gave_up": 0, "unreached": 0, "stale": 0, "errors": 0,
             "skipped": ""}
    if not settings.OWNER_RECOVERY_ENABLED:
        stats["skipped"] = "OWNER_RECOVERY_ENABLED is off"
        return stats
    lock = _acquire_lock()
    if lock is None:
        stats["skipped"] = "another tick is running"
        return stats
    try:
        return _tick(stats)
    finally:
        _release_lock(lock)


def _tick(stats: dict) -> dict:
    from src.db.session import system_sync_session
    from src.scrapers.enrichment.king_county_assessor import (
        KingOwnerLookupBlockedError,
        batch_extract_king_owners,
    )
    from src.scrapers.enrichment.source_health import (
        KING_EREALPROPERTY,
        SourceUnavailableError,
        is_source_available,
    )

    deadline = time.monotonic() + _TICK_BUDGET_S
    with system_sync_session() as db:
        if not is_source_available(db, KING_EREALPROPERTY):
            stats["skipped"] = "king_erealproperty is in cooldown"
            return stats
        params = {"max_attempts": _MAX_ATTEMPTS}
        parcels = [r.parcel_id for r in db.execute(
            sa_text(_CANDIDATE_PARCELS_SQL), {**params, "batch": _BATCH_PARCELS}).all()]
        if not parcels:
            db.rollback()
            return stats
        rows = db.execute(sa_text(_CANDIDATE_ROWS_SQL), {**params, "parcels": parcels}).all()
        db.rollback()  # release the read snapshot before any network I/O
        by_parcel: dict[str, list] = {}
        for row in rows:
            by_parcel.setdefault(row.parcel_id, []).append(row)
        stats["parcels"], stats["rows"] = len(parcels), len(rows)

        owners: dict[str, str] = {}
        o_stats: dict = {}
        try:
            asyncio.run(asyncio.wait_for(
                batch_extract_king_owners(
                    parcels, delay=_PACE_S, circuit_window=20, max_transient_rate=0.10,
                    max_unresolved_rate=0.50, fetch_attempts=1, out=owners, stats=o_stats,
                    time_budget_s=max(10.0, deadline - time.monotonic() - 30),
                ),
                timeout=max(30.0, deadline - time.monotonic()),
            ))
        except (KingOwnerLookupBlockedError, SourceUnavailableError) as exc:
            stats["skipped"] = f"stopped: {str(exc)[:120]}"
            _logger.warning("Owner recovery: %s", stats["skipped"])
        except Exception as exc:  # noqa: BLE001 -- best-effort background recovery
            stats["skipped"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            _logger.warning("Owner recovery: lookup failed: %s", stats["skipped"])

        for pid, outcome in _classify(parcels, owners, o_stats).items():
            for row in by_parcel.get(pid, []):
                stats[_write(db, row, outcome, owners.get(pid))] += 1

    _logger.info(
        "Owner recovery: %d parcel(s) / %d lead(s); leads: %d found, %d not on record, "
        "%d parcel mismatch, %d transient, %d gave up, %d unreached, %d stale, %d error%s",
        stats["parcels"], stats["rows"], stats["found"], stats["not_on_record"],
        stats["parcel_mismatch"], stats["transient"], stats["gave_up"], stats["unreached"],
        stats["stale"], stats["errors"], f" ({stats['skipped']})" if stats["skipped"] else "",
    )
    return stats


def _classify(parcels: list[str], owners: dict, o_stats: dict) -> dict[str, str]:
    """Exactly one outcome per requested parcel.

    `unreached` is derived from what was REQUESTED minus what the lookup reports it
    fetched, never from absence in one list: a parcel King answered in a way no
    list names is treated as transient (retried, charged) rather than silently
    rotated forever.
    """
    no_owner = set(o_stats.get("no_owner_on_record", []))
    mismatch = set(o_stats.get("parcel_mismatch", []))
    transient = set(o_stats.get("transient", []))
    fetched = set(o_stats.get("attempted", [])) | transient
    out: dict[str, str] = {}
    for pid in parcels:
        if pid in owners:
            out[pid] = "found"
        elif pid in no_owner:
            out[pid] = "not_on_record"
        elif pid in mismatch:
            out[pid] = "parcel_mismatch"
        elif pid in fetched:
            out[pid] = "transient"
        else:
            out[pid] = "unreached"
    return out


def _write(db, row, outcome: str, owner: str | None) -> str:
    """One guarded UPDATE per row. Returns the stats key for what was written:
    the outcome, `gave_up`, `stale` (the row changed since selection, nothing
    written) or `errors`."""
    ed = row.enrichment_data if isinstance(row.enrichment_data, dict) else {}
    try:
        attempts = int(ed.get(ATTEMPTS_KEY) or 0)
    except (TypeError, ValueError):
        attempts = 0
    payload: dict = {LAST_AT_KEY: _now_iso()}
    drop_key = "__none__"
    label = outcome
    if outcome == "unreached":
        pass                                     # not asked: rotate, charge nothing
    elif outcome == "found":
        payload.update({OWNER_DEFERRED_KEY: False, RECOVERY_OUTCOME_KEY: "found",
                        ATTEMPTS_KEY: attempts + 1})
        drop_key = OWNER_DEFERRED_REASON_KEY
    elif outcome == "not_on_record":
        payload.update({OWNER_DEFERRED_KEY: False, OWNER_OUTCOME_KEY: OWNER_NOT_ON_RECORD,
                        RECOVERY_OUTCOME_KEY: "not_on_record", ATTEMPTS_KEY: attempts + 1})
        drop_key = OWNER_DEFERRED_REASON_KEY
    elif outcome == "parcel_mismatch":
        # King answers with a different parcel for this id every time: settled.
        payload.update({OWNER_DEFERRED_KEY: False, OWNER_DEFERRED_REASON_KEY: "parcel_mismatch",
                        RECOVERY_OUTCOME_KEY: "parcel_mismatch", ATTEMPTS_KEY: attempts + 1})
    else:
        attempts += 1
        gave_up = attempts >= _MAX_ATTEMPTS
        payload.update({ATTEMPTS_KEY: attempts, OWNER_DEFERRED_KEY: not gave_up,
                        RECOVERY_OUTCOME_KEY: "gave_up" if gave_up else "transient_failure"})
        label = "gave_up" if gave_up else "transient"
    try:
        result = db.execute(sa_text(_WRITE_SQL), {
            "rid": row.id, "uid": row.user_id, "pid": row.parcel_id,
            "owner": owner if outcome == "found" else None,
            "drop_key": drop_key, "payload": json.dumps(payload),
            "max_attempts": _MAX_ATTEMPTS,
        })
        db.commit()
        return label if result.rowcount else "stale"
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        _logger.warning("Owner recovery: write failed for row %s: %s",
                        str(row.id)[:8], str(exc)[:160])
        return "errors"


try:  # pragma: no cover -- registration only
    from src.workers import app

    @app.task(name="src.workers.owner_recovery.recover_deferred_owners")
    def recover_deferred_owners() -> dict:
        """Beat entry point: see recover_deferred_king_owners."""
        return recover_deferred_king_owners()
except Exception as exc:  # pragma: no cover -- import-time safety only
    _logger.debug("Owner recovery task not registered: %s", str(exc)[:120])
