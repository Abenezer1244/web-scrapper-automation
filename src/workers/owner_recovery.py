"""Background recovery of King tax owner names a job could not look up.

WHY THIS EXISTS
---------------
King tax leads get their owner name from eRealProperty, one page per parcel,
under a shared source lease and a wall-clock budget. A job that loses the lease,
runs out of budget or meets a throttle leaves leads unnamed and (since #298) marks
each one `owner_lookup_deferred = true` with the reason. Job b2f2ecd5 delivered 840
King tax leads with 0 owner names; without this sweep they would stay that way
until someone re-ran the scraper. On 2026-10-12, 22,009 of 22,855 delivered King tax
leads were still unnamed, none of them flagged, so this sweep never saw them.

WHAT THIS IS NOT
----------------
An OWNER-NAME BACKFILL and nothing else, on the same boundary as the mailing
sweep in mailing_recovery.py:

  * It never creates a Job and never touches quota, billing, `reserved_count` or
    `records_used`.
  * It never enqueues a skip trace (Tracerfy keys off the property address, which
    this never changes) and never touches `skip_trace_status`, phone or email.
  * It only fills a `party_name` that is still blank, on a row that is still
    visible and not yet settled, on a terminal job. Each condition is re-checked
    in the UPDATE itself, so a row a re-run named, or the plan cap excluded, while
    the lookup was in flight is left alone.

WHICH LEADS
-----------
Every VISIBLE unnamed King tax lead (owner decision 2026-10-12): delivered rows the
plan cap did not mark over quota, AND the account's already-delivered rows (shown on
the run's "already delivered" list). Over-quota and same-run/superseded rows are
never shown, so never asked about. The deferred flag is not required: leads
delivered before #298 never got it.

ONE ANSWER PER PARCEL
---------------------
Every eRealProperty request costs goodwill with a source that has IP-blocked us
twice, and the same parcels return on every run for every account. So each settled
answer (found / not on record, from a page that echoed the requested PIN) is stored
in `king_parcel_owners` and names every waiting lead of that parcel in one UPDATE,
with no further request for `_FRESH_DAYS`. Stored answers are applied first on each
tick, so they keep naming leads while King is in cooldown. Parcels with a delivered
lead are asked first, then fewest attempts, then the largest balance.

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
import re
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

# Parcels per tick, at most 160 King requests an hour. It was 120 (480 an hour):
# on 2026-10-18 the sweeps alone ran King into its ExceedCount.aspx request cap
# and every King pass sat in cooldown for over a day. King publishes no number,
# so this is a guess; raise it only after a week with no ExceedCount.
_BATCH_PARCELS = 40
_PACE_S = 1.0
_TICK_BUDGET_S = 300.0

_LOCK_KEY = "bl:owner_recovery:lock"
_LOCK_TTL_S = 1200

# How long a stored eRealProperty answer (king_parcel_owners) stands in for asking
# King again. Ownership changes on a sale, so this is a trade between a stale name
# and King's throttle: at 7 days every weekly King tax run would re-ask ~15,000
# parcels, the load that trips it (Codex suggested 7; 30 chosen, 2026-10-12).
_FRESH_DAYS = 30
# Leads filled from stored answers per tick: one set-based UPDATE, no King request.
_CACHE_FILL_ROWS = 5000

# Visible King tax leads still waiting for an owner: delivered leads AND the
# account's already-delivered rows (owner decision 2026-10-12: fill every unnamed
# lead, once per property). A lead is waiting until one of its answers settles it:
# found (named), not on record, a parcel King answers for with another PIN, or
# out of attempts. The deferred flag is not required: leads delivered before the
# job learned to set it (22,009 on 2026-10-12) never got it. The same predicate
# guards every write, so selection and write cannot disagree.
_ELIGIBLE_ROW = """
      (r.is_duplicate = false
       OR (r.is_duplicate = true AND coalesce(r.duplicate_reason, 'prior_run') = 'prior_run'))
  AND r.enrichment_data->>'delivery_excluded_reason' IS NULL
  AND coalesce(r.enrichment_data->>'owner_lookup_outcome', '') <> 'not_on_record'
  AND coalesce(r.enrichment_data->>'owner_recovery_outcome', '')
      NOT IN ('found', 'not_on_record', 'parcel_mismatch', 'gave_up')
  AND (r.party_name IS NULL OR btrim(r.party_name) = '')
  AND btrim(r.parcel_id) ~ '^[0-9]{10}$'
  AND (CASE WHEN r.enrichment_data->>'owner_recovery_attempts' ~ '^[0-9]{1,6}$'
            THEN (r.enrichment_data->>'owner_recovery_attempts')::int ELSE 0 END) < :max_attempts
"""

_KING_TAX_JOB = """
      j.status = 'done'
  AND lower(sc.county) = 'king' AND upper(sc.state) = 'WA'
  AND sc.record_type = 'tax_delinquent'
"""

# Pick PARCELS, not rows (two leads on one parcel share one lookup): parcels with
# a delivered lead first, then fewest attempts, the longest since last tried, the
# largest balance. A parcel with a fresh stored answer is never asked again; the
# cache fill names its leads instead.
_CANDIDATE_PARCELS_SQL = f"""
    SELECT btrim(r.parcel_id) AS parcel_id,
           bool_or(r.is_duplicate = false) AS delivered,
           min(CASE WHEN r.enrichment_data->>'owner_recovery_attempts' ~ '^[0-9]{1,6}$'
                    THEN (r.enrichment_data->>'owner_recovery_attempts')::int ELSE 0 END) AS attempts,
           min(coalesce(r.enrichment_data->>'owner_recovery_last_at', '')) AS last_at,
           max(r.delinquent_amount) AS amount
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE {_KING_TAX_JOB}
      AND {_ELIGIBLE_ROW}
      AND NOT EXISTS (
        SELECT 1 FROM king_parcel_owners k
        WHERE k.parcel_id = btrim(r.parcel_id)
          AND k.observed_at > now() - make_interval(days => :fresh_days))
    GROUP BY btrim(r.parcel_id)
    ORDER BY delivered DESC, attempts ASC, last_at ASC, amount DESC NULLS LAST, parcel_id ASC
    LIMIT :batch
"""  # noqa: S608 -- splices only constant SQL fragments; every value is bound

# Name every waiting lead whose parcel has a fresh stored answer, in one statement
# and without asking King. Same eligibility as every other write; the payload is
# what `_write` stores for the same outcome. RETURNING feeds the stats.
_CACHE_FILL_SQL = f"""
    UPDATE results r SET
      party_name = CASE WHEN k.outcome = 'found' THEN k.owner_name ELSE r.party_name END,
      enrichment_data = (
        ((CASE WHEN jsonb_typeof(r.enrichment_data::jsonb) = 'object'
               THEN r.enrichment_data::jsonb ELSE '{{}}'::jsonb END)
         - 'owner_lookup_deferred_reason')
        || jsonb_build_object('owner_lookup_deferred', false,
                              'owner_recovery_outcome', k.outcome,
                              'owner_recovery_last_at', CAST(:now_iso AS text))
        -- Charged only when King was asked for this lead's parcel on this tick; a
        -- lead named from an earlier answer cost no request.
        || CASE WHEN CAST(:count_attempt AS boolean)
                THEN jsonb_build_object('owner_recovery_attempts',
                  (CASE WHEN r.enrichment_data->>'owner_recovery_attempts' ~ '^[0-9]{1,6}$'
                        THEN (r.enrichment_data->>'owner_recovery_attempts')::int ELSE 0 END) + 1)
                ELSE '{{}}'::jsonb END
        || CASE WHEN k.outcome = 'not_on_record'
                THEN jsonb_build_object('owner_lookup_outcome', 'not_on_record')
                ELSE '{{}}'::jsonb END)::json
    FROM king_parcel_owners k
    WHERE r.id IN (
        SELECT r.id FROM results r
        JOIN jobs j ON j.id = r.job_id
        JOIN scraper_configs sc ON sc.id = j.scraper_config_id
        JOIN king_parcel_owners k ON k.parcel_id = btrim(r.parcel_id)
        WHERE {_KING_TAX_JOB}
          AND {_ELIGIBLE_ROW}
          AND k.observed_at > now() - make_interval(days => :fresh_days)
          AND (CAST(:parcels AS text[]) IS NULL OR k.parcel_id = ANY(CAST(:parcels AS text[])))
        ORDER BY r.id
        LIMIT :fill_rows)
      AND k.parcel_id = btrim(r.parcel_id)
      AND k.observed_at > now() - make_interval(days => :fresh_days)
      AND {_ELIGIBLE_ROW}
      AND EXISTS (
        SELECT 1 FROM jobs j JOIN scraper_configs sc ON sc.id = j.scraper_config_id
        WHERE j.id = r.job_id AND {_KING_TAX_JOB})
    RETURNING k.outcome
"""  # noqa: S608 -- splices only constant SQL fragments; every value is bound

# Store one settled eRealProperty answer for its parcel. A newer observation wins;
# an older one never overwrites it.
_STORE_SQL = """
    INSERT INTO king_parcel_owners (parcel_id, outcome, owner_name, observed_at, source)
    VALUES (:pid, :outcome, :owner, now(), 'erealproperty')
    ON CONFLICT (parcel_id) DO UPDATE SET
      outcome = EXCLUDED.outcome, owner_name = EXCLUDED.owner_name,
      observed_at = EXCLUDED.observed_at, source = EXCLUDED.source
    WHERE EXCLUDED.observed_at > king_parcel_owners.observed_at
"""

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


# Delete the lock only if this tick still owns it. A tick that outlived the TTL must
# not remove the lock a newer tick now holds.
_RELEASE_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""


def _acquire_lock() -> tuple | str:
    """Single-flight lock: (client, token) when held, otherwise the reason to skip.

    Fails CLOSED. Without the lock two ticks could pick the same parcels and send
    King the same requests twice; a skipped tick costs nothing but 15 minutes.
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


def _release_lock(lock: tuple) -> None:
    client, token = lock
    try:
        client.eval(_RELEASE_IF_OWNER, 1, _LOCK_KEY, token)
    except Exception:  # noqa: BLE001, S110 -- the TTL releases it anyway
        pass


def recover_deferred_king_owners() -> dict:
    """One bounded tick. Returns a stats dict (also read by the tests)."""
    stats = {"parcels": 0, "rows": 0, "found": 0, "not_on_record": 0, "parcel_mismatch": 0,
             "transient": 0, "gave_up": 0, "unreached": 0, "stale": 0, "errors": 0,
             "cached": 0, "skipped": ""}
    if not settings.OWNER_RECOVERY_ENABLED:
        stats["skipped"] = "OWNER_RECOVERY_ENABLED is off"
        return stats
    lock = _acquire_lock()
    if isinstance(lock, str):
        stats["skipped"] = lock
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
        # Stored answers first: no King request, so this runs during a cooldown too.
        cached = _fill_from_store(db)
        stats["cached"] = cached["found"] + cached["not_on_record"]
        stats["errors"] += cached["errors"]
        if not is_source_available(db, KING_EREALPROPERTY):
            stats["skipped"] = "king_erealproperty is in cooldown"
            return stats
        params = {"max_attempts": _MAX_ATTEMPTS, "fresh_days": _FRESH_DAYS}
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
        # Ask King only about parcels that still have an eligible row after the
        # second read; one that lost its rows in between is not worth a request.
        parcels = [p for p in parcels if p in by_parcel]
        if not parcels:
            return stats
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

        stored: list[str] = []
        for pid, outcome in _classify(parcels, owners, o_stats).items():
            # A settled answer is stored once for the parcel and fills its leads in
            # one statement below; anything else is charged row by row as before.
            if outcome in ("found", "not_on_record") and _store(db, pid, outcome, owners.get(pid)):
                stored.append(pid)
                continue
            for row in by_parcel.get(pid, []):
                stats[_write(db, row, outcome, owners.get(pid))] += 1
        if stored:
            filled = _fill_from_store(db, stored)
            for outcome, n in filled.items():
                stats[outcome] += n
            # A lead selected for these parcels but not written changed during the
            # lookup (named by a re-run, capped, its job reopened): left alone.
            expected = sum(len(by_parcel[p]) for p in stored)
            if expected <= _CACHE_FILL_ROWS and not filled["errors"]:
                stats["stale"] += max(0, expected - filled["found"] - filled["not_on_record"])

    _logger.info(
        "Owner recovery: %d named from stored answers; %d parcel(s) / %d lead(s) asked; "
        "leads: %d found, %d not on record, "
        "%d parcel mismatch, %d transient, %d gave up, %d unreached, %d stale, %d error%s",
        stats["cached"], stats["parcels"], stats["rows"], stats["found"], stats["not_on_record"],
        stats["parcel_mismatch"], stats["transient"], stats["gave_up"], stats["unreached"],
        stats["stale"], stats["errors"], f" ({stats['skipped']})" if stats["skipped"] else "",
    )
    return stats


def _store(db, pid: str, outcome: str, owner: str | None) -> bool:
    """Store one settled eRealProperty answer for its parcel. False when it could not
    be stored, so the caller falls back to writing the parcel's leads one by one."""
    name = (owner or "").strip() if outcome == "found" else None
    if outcome == "found" and not name:
        return False
    try:
        db.execute(sa_text(_STORE_SQL), {"pid": pid, "outcome": outcome, "owner": name})
        db.commit()
        return True
    except Exception as exc:  # noqa: BLE001 -- the row-by-row write still runs
        db.rollback()
        _logger.warning("Owner recovery: answer for parcel %s not stored: %s",
                        pid[-4:], type(exc).__name__)
        return False


# Fresh stored answers for a set of parcels: what the scrape job reads before it asks King.
_FRESH_SQL = """
    SELECT parcel_id, outcome, owner_name FROM king_parcel_owners
    WHERE parcel_id = ANY(CAST(:pids AS text[]))
      AND observed_at > now() - make_interval(days => :fresh_days)
"""


def _is_pin(pid: str | None) -> bool:
    return bool(pid) and re.fullmatch(r"[0-9]{10}", pid) is not None


def fresh_answers(db, parcels) -> dict[str, tuple[str, str | None]]:
    """{pid: (outcome, owner_name)} for every parcel with a fresh stored answer.

    Read by the scrape job (enrich.py) before its King page passes, so a parcel
    answered in the last `_FRESH_DAYS` is named with no request. Only a 10-digit
    PIN can have an answer; anything else is never looked up here."""
    pins = sorted({p for p in parcels if _is_pin(p)})
    if not pins:
        return {}
    rows = db.execute(sa_text(_FRESH_SQL), {"pids": pins, "fresh_days": _FRESH_DAYS}).all()
    return {row.parcel_id: (row.outcome, row.owner_name) for row in rows}


def store_answers(db, answers: dict[str, tuple[str, str | None]]) -> int:
    """Store the scrape job's settled eRealProperty answers, {pid: (outcome, owner)}.

    Only answers from a page that echoed the requested 10-digit PIN belong here; the
    caller passes nothing else. One statement, one commit; call it with nothing else
    pending, because a failure rolls back. Returns answers sent, 0 on failure (the
    job's own leads are already written; the beat sweep asks King later)."""
    params = []
    for pid, (outcome, owner) in answers.items():
        name = (owner or "").strip() if outcome == "found" else None
        if not _is_pin(pid) or outcome not in ("found", "not_on_record"):
            continue
        if outcome == "found" and not name:
            continue
        params.append({"pid": pid, "outcome": outcome, "owner": name})
    if not params:
        return 0
    try:
        db.execute(sa_text(_STORE_SQL), params)
        db.commit()
    except Exception as exc:  # noqa: BLE001 -- the answers are on the job's leads already
        db.rollback()
        _logger.warning("Owner store: %d answers from the job not stored: %s",
                        len(params), type(exc).__name__)
        return 0
    return len(params)


def _fill_from_store(db, parcels: list[str] | None = None) -> dict[str, int]:
    """Name waiting leads from fresh stored answers. One UPDATE, no King request.

    `parcels` None: any parcel (the start of a tick; nothing is charged). A list: the
    parcels King just answered on this tick, so each lead is charged its attempt as
    `_write` would. Returns rows written per outcome, and `errors` 1 if the fill failed."""
    counts = {"found": 0, "not_on_record": 0, "errors": 0}
    try:
        written = db.execute(sa_text(_CACHE_FILL_SQL), {
            "max_attempts": _MAX_ATTEMPTS, "fresh_days": _FRESH_DAYS,
            "fill_rows": _CACHE_FILL_ROWS, "now_iso": _now_iso(), "parcels": parcels,
            "count_attempt": parcels is not None,
        }).scalars().all()
        db.commit()
    except Exception as exc:  # noqa: BLE001 -- the next tick tries again
        db.rollback()
        _logger.warning("Owner recovery: fill from stored answers failed: %s",
                        type(exc).__name__)
        counts["errors"] = 1
        return counts
    for outcome in written:
        counts[outcome] += 1
    return counts


def _classify(parcels: list[str], owners: dict, o_stats: dict) -> dict[str, str]:
    """Exactly one outcome per requested parcel.

    `unreached` is derived from what was REQUESTED minus what the lookup reports it
    fetched, never from absence in one list: a parcel King answered in a way no
    list names is treated as transient (retried, charged) rather than silently
    rotated forever.

    Every normal exit of the lookup records an `outcome`. None means it was cut
    off mid-request (an exception or the outer timeout). It walks parcels in order,
    one at a time, so the first parcel it has no record of is the one in flight:
    charge it as transient, or a parcel that crashes the lookup would retry for
    free on every tick.
    """
    no_owner = set(o_stats.get("no_owner_on_record", []))
    mismatch = set(o_stats.get("parcel_mismatch", []))
    transient = set(o_stats.get("transient", []))
    fetched = set(o_stats.get("attempted", [])) | transient
    if o_stats.get("outcome") is None:
        in_flight = next((p for p in parcels if p not in fetched and p not in owners), None)
        if in_flight is not None:
            fetched.add(in_flight)
    out: dict[str, str] = {}
    for pid in parcels:
        if (owners.get(pid) or "").strip():
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
    # Loud: beat would keep publishing a task nobody runs.
    _logger.error("Owner recovery task NOT registered: %s", str(exc)[:120])
