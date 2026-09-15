"""Background owner names for King (Seattle SDCI) code-violation leads.

WHY THIS EXISTS
---------------
SDCI names the complaint, never the owner. A King code-violation job locates each
case's parcel (`enrichment_data.kc_pin`) and names the owner of a shown location
(exact or street-level, src/utils/located_parcel.py) from eRealProperty inside a
240 s budget. At 1 request per second a large job runs out of budget, or loses the
shared King lease, and every row it did not reach stays unnamed: nothing else ever
looked at it again except a manual run of
scripts/backfill_king_code_violation_owner.py. This sweep is that second look.

WHAT THIS IS NOT
----------------
An OWNER-NAME FILL and nothing else, on the same boundary as owner_recovery.py:

  * It never creates a Job and never touches quota, billing, `reserved_count` or
    `records_used`.
  * It never changes `parcel_id`, `dedup_hash`, `property_key`, mailing or
    `skip_trace_status`, and never enqueues a skip trace.
  * It only fills a `party_name` that is still blank, on a row that is still
    delivered, still located on the SAME parcel at a shown tier, with no owner
    source yet, on a job that is still done. Each condition is re-checked in the
    UPDATE itself, so a row a re-run named, re-located or the plan cap excluded
    while the lookup was in flight is left alone.

WHICH LEADS
-----------
Delivered leads only: non-duplicate rows the plan cap did not mark over quota,
with a usable property or mailing address (src/api/lead_actionability.py).
One lookup per PARCEL (two cases on one parcel share it): fewest attempts first,
then the longest since last tried, then the newest case.

KING RATE LIMITS
----------------
The lookup runs through `batch_extract_king_owners`: the shared SourceAdmission
lease (one King eRealProperty pass at a time across jobs and every sweep), the
source-health gate, the owner breaker, and the parcel-echo check that drops a page
the county served for another parcel. It paces 1 request per second inside the
lease, and `OWNER_RECOVERY_ENABLED` stops it before any request.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime

from celery.exceptions import SoftTimeLimitExceeded, TimeLimitExceeded
from sqlalchemy import text as sa_text

from src.api.lead_actionability import actionable_sql
from src.config import settings
from src.utils.located_parcel import located_parcel_id, shown_tier_sql
from src.utils.logger import setup_logger

_logger = setup_logger("worker.cv_owner_recovery")

SDCI_SOURCE = "seattle_sdci_code_violations"
ATTEMPTS_KEY = "cv_owner_recovery_attempts"
LAST_AT_KEY = "cv_owner_recovery_last_at"
RECOVERY_OUTCOME_KEY = "cv_owner_recovery_outcome"

# A transient failure is retried this many times across ticks, then the lead is
# settled as `gave_up` so a permanently failing parcel cannot hold its place.
_MAX_ATTEMPTS = 5

# Parcels per tick. At `_PACE_S` that is about two minutes of requests, inside
# `_TICK_BUDGET_S`, and small against the 20-minute schedule.
_BATCH_PARCELS = 120
_PACE_S = 1.0
_TICK_BUDGET_S = 300.0
# The writes after the lookup get their own bound, and each UPDATE its own timeout.
_WRITE_BUDGET_S = 120.0
_WRITE_STATEMENT_TIMEOUT_MS = 10000

# Never swallowed: the worker is ending the task.
_CELERY_TIME_LIMITS = (SoftTimeLimitExceeded, TimeLimitExceeded)

_LOCK_KEY = "bl:cv_owner_recovery:lock"
_LOCK_TTL_S = 1200

# Delivered King code-violation leads still waiting for an owner, located at a
# shown tier. Mirrors src/utils/located_parcel.py located_parcel_id (kc_pin a
# 10-digit string, status matched, and a shown tier with the source allowed to
# produce it: exact/street_only from the point rule, address_point from the address
# points; never address_only or condo_complex). The tier predicate is generated from
# located_parcel.py so SQL and Python cannot disagree; the Python rule is checked
# again before a lookup. The same predicate guards every write.
_ELIGIBLE_ROW = """
      r.is_duplicate = false
  AND jsonb_typeof(r.enrichment_data::jsonb) = 'object'
  AND r.enrichment_data::jsonb->>'delivery_excluded_reason' IS NULL
  AND (r.party_name IS NULL OR r.party_name ~ '^[[:space:]]*$')
  AND r.enrichment_data::jsonb->>'source' = 'seattle_sdci_code_violations'
  AND r.enrichment_data::jsonb->>'kc_pin_status' = 'matched'
""" + f"""  AND {shown_tier_sql("r.enrichment_data::jsonb")}
""" + """  AND jsonb_typeof(r.enrichment_data::jsonb->'kc_pin') = 'string'
  AND r.enrichment_data::jsonb->>'kc_pin' ~ '^[0-9]{10}$'
  AND NOT (r.enrichment_data::jsonb ? 'owner_source')
  AND coalesce(r.enrichment_data::jsonb->>'cv_owner_recovery_outcome', '')
      NOT IN ('not_on_record', 'parcel_mismatch', 'gave_up')
  AND (CASE WHEN r.enrichment_data::jsonb->>'cv_owner_recovery_attempts' ~ '^[0-9]{1,6}$'
            THEN (r.enrichment_data::jsonb->>'cv_owner_recovery_attempts')::int ELSE 0 END)
      < :max_attempts
""" + f"""  AND {actionable_sql("r")}
"""  # a row with no usable address is not a lead: never shown, exported or billed

_KING_CV_JOB = """
      j.status = 'done'
  AND j.user_id = r.user_id AND sc.user_id = j.user_id
  AND lower(sc.county) = 'king' AND upper(sc.state) = 'WA'
  AND sc.record_type = 'code_violation'
"""

# Pick PARCELS, not rows: fewest attempts first, then the longest since last tried,
# then the newest case.
_CANDIDATE_PARCELS_SQL = f"""
    SELECT r.enrichment_data::jsonb->>'kc_pin' AS pin,
           min(CASE WHEN r.enrichment_data::jsonb->>'cv_owner_recovery_attempts' ~ '^[0-9]{{1,6}}$'
                    THEN (r.enrichment_data::jsonb->>'cv_owner_recovery_attempts')::int
                    ELSE 0 END) AS attempts,
           min(coalesce(r.enrichment_data::jsonb->>'cv_owner_recovery_last_at', '')) AS last_at,
           max(r.date_recorded_parsed) AS newest
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE {_KING_CV_JOB}
      AND {_ELIGIBLE_ROW}
    GROUP BY r.enrichment_data::jsonb->>'kc_pin'
    ORDER BY attempts ASC, last_at ASC, newest DESC NULLS LAST, pin ASC
    LIMIT :batch
"""  # noqa: S608 -- splices only module constants; every value is bound

_CANDIDATE_ROWS_SQL = f"""
    SELECT r.id, r.user_id, r.enrichment_data::jsonb->>'kc_pin' AS pin, r.enrichment_data
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE {_KING_CV_JOB}
      AND {_ELIGIBLE_ROW}
      AND r.enrichment_data::jsonb->>'kc_pin' = ANY(:pins)
    ORDER BY r.id
"""  # noqa: S608 -- splices only module constants; every value is bound

# The write re-applies the eligibility predicate against the row AND its job as they
# are NOW, pinned to the parcel the lookup was made for.
_WRITE_SQL = f"""
    UPDATE results r SET
      party_name = COALESCE(CAST(:owner AS varchar), r.party_name),
      enrichment_data = (r.enrichment_data::jsonb || CAST(:payload AS jsonb))::json
    WHERE r.id = :rid AND r.user_id = :uid
      AND r.enrichment_data::jsonb->>'kc_pin' = :pin
      AND {_ELIGIBLE_ROW}
      AND EXISTS (
        SELECT 1 FROM jobs j JOIN scraper_configs sc ON sc.id = j.scraper_config_id
        WHERE j.id = r.job_id AND {_KING_CV_JOB})
"""  # noqa: S608 -- splices only module constants; every value is bound


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# Delete the lock only if this tick still owns it. A tick that outlived the TTL must
# not remove the lock a newer tick now holds.
_RELEASE_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""


def _acquire_lock() -> tuple | str:
    """Single-flight lock: (client, token) when held, otherwise the reason to skip.

    Fails CLOSED. Without the lock two ticks could pick the same parcels and send
    King the same requests twice; a skipped tick costs nothing but 20 minutes.
    """
    import uuid

    try:
        import redis as sync_redis

        client = sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
        token = uuid.uuid4().hex
        if client.set(_LOCK_KEY, token, nx=True, ex=_LOCK_TTL_S):
            return client, token
        return "another tick is running"
    except Exception as exc:  # noqa: BLE001
        return f"lock unavailable: {type(exc).__name__}"


# Extend the lock only if this tick still owns it.
_RENEW_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) end
return 0
"""


def _renew_lock(lock: tuple) -> bool:
    """True while this tick still owns the lock (and its TTL is reset).

    Checked before every parcel's writes: a tick that outlived its TTL, or lost
    Redis, must not write alongside a newer tick that re-selected the same rows.
    """
    client, token = lock
    try:
        return bool(client.eval(_RENEW_IF_OWNER, 1, _LOCK_KEY, token, _LOCK_TTL_S))
    except Exception:  # noqa: BLE001 -- fail closed: no proof of ownership, no write
        return False


def _release_lock(lock: tuple) -> None:
    client, token = lock
    try:
        client.eval(_RELEASE_IF_OWNER, 1, _LOCK_KEY, token)
    except Exception:  # noqa: BLE001, S110 -- the TTL releases it anyway
        pass


def recover_code_violation_owners() -> dict:
    """One bounded tick. Returns a stats dict (also read by the tests)."""
    stats = {"parcels": 0, "rows": 0, "found": 0, "not_on_record": 0, "parcel_mismatch": 0,
             "transient": 0, "gave_up": 0, "unreached": 0, "stale": 0, "errors": 0,
             "skipped": ""}
    if not settings.OWNER_RECOVERY_ENABLED:
        stats["skipped"] = "OWNER_RECOVERY_ENABLED is off"
        return stats
    lock = _acquire_lock()
    if isinstance(lock, str):
        stats["skipped"] = lock
        return stats
    try:
        return _tick(stats, lock)
    finally:
        _release_lock(lock)


def _tick(stats: dict, lock: tuple) -> dict:
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
        pins = [r.pin for r in db.execute(
            sa_text(_CANDIDATE_PARCELS_SQL), {**params, "batch": _BATCH_PARCELS}).all()]
        if not pins:
            db.rollback()
            return stats
        rows = db.execute(sa_text(_CANDIDATE_ROWS_SQL), {**params, "pins": pins}).all()
        db.rollback()  # release the read snapshot before any network I/O
        by_pin: dict[str, list] = {}
        for row in rows:
            # The read-side rule that shows this PIN as the lead's parcel must agree
            # before its owner is looked up; SQL alone is not trusted for a name.
            if located_parcel_id(row.enrichment_data) == row.pin:
                by_pin.setdefault(row.pin, []).append(row)
        # Ask King only about parcels that still have an eligible row after the
        # second read; one that lost its rows in between is not worth a request.
        pins = [p for p in pins if p in by_pin]
        if not pins:
            return stats
        stats["parcels"], stats["rows"] = len(pins), sum(len(v) for v in by_pin.values())

        owners: dict[str, str] = {}
        o_stats: dict = {}
        try:
            asyncio.run(asyncio.wait_for(
                batch_extract_king_owners(
                    pins, delay=_PACE_S, circuit_window=20, max_transient_rate=0.10,
                    max_unresolved_rate=0.50, fetch_attempts=1, out=owners, stats=o_stats,
                    time_budget_s=max(10.0, deadline - time.monotonic() - 30),
                ),
                timeout=max(30.0, deadline - time.monotonic()),
            ))
        except _CELERY_TIME_LIMITS:
            raise                                # the worker is ending this task: stop now
        except (KingOwnerLookupBlockedError, SourceUnavailableError) as exc:
            stats["skipped"] = f"stopped: {str(exc)[:120]}"
            _logger.warning("Code violation owner recovery: %s", stats["skipped"])
        except Exception as exc:  # noqa: BLE001 -- best-effort background recovery
            stats["skipped"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            _logger.warning("Code violation owner recovery: lookup failed: %s", stats["skipped"])

        outcomes = _classify(pins, owners, o_stats)
        # Bounded write phase. Ownership is proven before EVERY write, not once per
        # parcel (a parcel can carry any number of rows), and the phase stops at its
        # own budget. A row not written keeps its state: no name, no charge, and it is
        # selected again next tick.
        write_deadline = time.monotonic() + _WRITE_BUDGET_S
        stop = ""
        for pin, outcome in outcomes.items():
            for row in by_pin.get(pin, []):
                if time.monotonic() >= write_deadline:
                    stop = "write budget exhausted"
                elif not _renew_lock(lock):
                    stop = "lock lost before writing"
                if stop:
                    break
                stats[_write(db, row, outcome, owners.get(pin))] += 1
            if stop:
                stats["skipped"] = stop
                _logger.warning("Code violation owner recovery: %s", stop)
                break

    _logger.info(
        "Code violation owner recovery: %d parcel(s) / %d lead(s); leads: %d found, "
        "%d not on record, %d parcel mismatch, %d transient, %d gave up, %d unreached, "
        "%d stale, %d error%s",
        stats["parcels"], stats["rows"], stats["found"], stats["not_on_record"],
        stats["parcel_mismatch"], stats["transient"], stats["gave_up"], stats["unreached"],
        stats["stale"], stats["errors"], f" ({stats['skipped']})" if stats["skipped"] else "",
    )
    return stats


def _classify(pins: list[str], owners: dict, o_stats: dict) -> dict[str, str]:
    """Exactly one outcome per requested parcel, charging only PROVEN requests.

    A parcel counts as asked only when the lookup's ledger says its fetch settled
    (`attempted` or `transient`, written after each fetch returns). Everything else,
    including the parcel in flight when the lookup raised or was cancelled, and every
    parcel when it failed before its first request, is `unreached`: rotated to the
    back of the queue, never charged an attempt. (The King tax sweep charges the
    in-flight parcel; here an unproven request must not walk a lead to gave_up.)
    A page with a blank name is not a found owner.
    """
    no_owner = set(o_stats.get("no_owner_on_record", []))
    mismatch = set(o_stats.get("parcel_mismatch", []))
    reached = set(o_stats.get("attempted", [])) | set(o_stats.get("transient", []))
    out: dict[str, str] = {}
    for pin in pins:
        if (owners.get(pin) or "").strip():
            out[pin] = "found"
        elif pin not in reached:
            out[pin] = "unreached"
        elif pin in no_owner:
            out[pin] = "not_on_record"
        elif pin in mismatch:
            out[pin] = "parcel_mismatch"
        else:
            out[pin] = "transient"
    return out


def _write(db, row, outcome: str, owner: str | None) -> str:
    """One guarded UPDATE per row. Returns the stats key for what was written:
    the outcome, `gave_up`, `stale` (the row changed since selection, nothing
    written) or `errors`."""
    ed = row.enrichment_data if isinstance(row.enrichment_data, dict) else {}
    if located_parcel_id(ed) != row.pin:
        return "stale"
    if outcome == "found" and not (owner or "").strip():
        outcome = "transient"                    # a blank name is not an owner
    try:
        attempts = int(ed.get(ATTEMPTS_KEY) or 0)
    except (TypeError, ValueError):
        attempts = 0
    now = _now_iso()
    payload: dict = {LAST_AT_KEY: now}
    name = None
    label = outcome
    if outcome == "unreached":
        pass                                     # not asked: rotate, charge nothing
    elif outcome == "found":
        name = (owner or "").strip()[:512]
        payload.update({"owner_source": _owner_source(), "owner_pin": row.pin,
                        "owner_checked_at": now, RECOVERY_OUTCOME_KEY: "found",
                        ATTEMPTS_KEY: attempts + 1})
    elif outcome in ("not_on_record", "parcel_mismatch"):
        # The county answered for this parcel (or always for another one): settled.
        # Never a name, never an owner_source.
        payload.update({RECOVERY_OUTCOME_KEY: outcome, ATTEMPTS_KEY: attempts + 1})
    else:
        attempts += 1
        gave_up = attempts >= _MAX_ATTEMPTS
        payload.update({ATTEMPTS_KEY: attempts,
                        RECOVERY_OUTCOME_KEY: "gave_up" if gave_up else "transient_failure"})
        label = "gave_up" if gave_up else "transient"
    try:
        # A row locked by another writer must not stall the tick for the session's
        # default statement timeout.
        db.execute(sa_text(f"SET LOCAL statement_timeout = '{_WRITE_STATEMENT_TIMEOUT_MS}'"))
        result = db.execute(sa_text(_WRITE_SQL), {
            "rid": row.id, "uid": row.user_id, "pin": row.pin,
            "owner": name or None, "payload": json.dumps(payload),
            "max_attempts": _MAX_ATTEMPTS,
        })
        db.commit()
        return label if result.rowcount else "stale"
    except _CELERY_TIME_LIMITS:
        db.rollback()
        raise
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        _logger.warning("Code violation owner recovery: write failed for row %s: %s",
                        str(row.id)[:8], str(exc)[:160])
        return "errors"


def _owner_source() -> str:
    from src.scrapers.enrichment.king_parcel_locate import OWNER_SOURCE

    return OWNER_SOURCE


try:  # pragma: no cover -- registration only
    from src.workers import app

    @app.task(name="src.workers.cv_owner_recovery.recover_code_violation_owners_task")
    def recover_code_violation_owners_task() -> dict:
        """Beat entry point: see recover_code_violation_owners."""
        return recover_code_violation_owners()
except Exception as exc:  # pragma: no cover -- import-time safety only
    # Loud: beat would keep publishing a task nobody runs. Same swallow-and-log
    # shape as owner_recovery.py, plus the traceback so the cause is visible.
    _logger.error("Code violation owner recovery task NOT registered: %s", str(exc)[:120],
                  exc_info=exc)
