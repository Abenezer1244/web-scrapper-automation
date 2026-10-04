"""Background mailing addresses for King code-violation leads the job never located.

WHY THIS EXISTS
---------------
Seattle SDCI code violations carry coordinates but no parcel. A King code-violation
job locates each lead's parcel on King's parcel layer (strict rule:
src/scrapers/enrichment/king_parcel_locate.py) and takes the mailing from the
Assessor extract, inside a 420 s budget (src/workers/tasks_helpers/enrich.py). A large
job runs out of budget: job 37014cb9 (2026-09-27, 1,756 rows) located ~300 and left
~800 with no status, so its mailing coverage read 36% against a 65% baseline. Nothing
looked at those rows again except a manual run of
scripts/backfill_king_code_violation_mailing.py. This sweep is that second look, and
because cv_owner_recovery only names leads that are located, it unlocks their owner
names too.

WHAT THIS IS NOT
----------------
A LOCATE + MAILING FILL and nothing else, on the same boundary as the backfill:
  * It never creates a Job and never touches quota, billing, `parcel_id` (the located
    PIN lives in enrichment_data.kc_pin), `dedup_hash`, `property_key` or skip trace.
  * It only writes a row that still has no mailing, no parcel_id and no
    `kc_pin_status`, on a job that is still done. Each condition is re-checked in the
    UPDATE itself.
  * A row the step decides gets `kc_pin_status` and is never asked again. A row with no
    answer is charged one attempt and settles as `gave_up` after `_MAX_ATTEMPTS`.

RATE LIMITS
-----------
Paced requests (`_PACE_S`), one tick at a time (Redis single-flight lock), a bounded
batch and budget per tick. A tick that gets no answer at all for a full batch marks the
step unhealthy in `external_source_health`, so the next ticks stand down on the
cooldown ladder instead of asking again every hour. `GIS_ENRICHMENT_ENABLED` stops it
before any request.
"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from celery.exceptions import SoftTimeLimitExceeded, TimeLimitExceeded
from sqlalchemy import text as sa_text

from src.config import settings
from src.scrapers.king_cv_sources import PARCEL_AT_SCRAPE_SOURCES
from src.utils.logger import setup_logger

_logger = setup_logger("worker.cv_mailing_recovery")

ATTEMPTS_KEY = "cv_mailing_recovery_attempts"
LAST_AT_KEY = "cv_mailing_recovery_last_at"
OUTCOME_KEY = "cv_mailing_recovery_outcome"

_MAX_ATTEMPTS = 5
# Rows per tick. Leads at one point share one lookup, so this is an upper bound on
# requests: about a minute of parcel-layer calls at `_PACE_S`, well inside the budget.
_BATCH_ROWS = 150
_PACE_S = 0.35
_TICK_BUDGET_S = 300.0
# A batch at least this big that gets no answer at all means the step is refusing us.
_BREAKER_MIN_ROWS = 10

_CELERY_TIME_LIMITS = (SoftTimeLimitExceeded, TimeLimitExceeded)

_LOCK_KEY = "bl:cv_mailing_recovery:lock"
_LOCK_TTL_S = 900

# Sources that print the King PIN at scrape take the ordinary parcel-keyed passes, never
# this locate step (same exclusion as enrich.py). Module constants, no quote characters.
_PRINTED_SOURCES_SQL = ", ".join(f"'{k}'" for k in sorted(PARCEL_AT_SCRAPE_SOURCES))

# Shared by the selection and the write guard, so they cannot disagree.
_ELIGIBLE_ROW = f"""
      r.parcel_id IS NULL
  AND r.mailing_address IS NULL
  AND jsonb_typeof(r.enrichment_data::jsonb) = 'object'
  AND r.enrichment_data::jsonb ? 'latitude'
  AND r.enrichment_data::jsonb ? 'longitude'
  AND NOT (r.enrichment_data::jsonb ? 'kc_pin_status')
  AND coalesce(r.enrichment_data::jsonb->>'source', '') NOT IN ({_PRINTED_SOURCES_SQL})
  AND coalesce(r.enrichment_data::jsonb->>'{OUTCOME_KEY}', '') <> 'gave_up'
  AND (CASE WHEN r.enrichment_data::jsonb->>'{ATTEMPTS_KEY}' ~ '^[0-9]{{1,6}}$'
            THEN (r.enrichment_data::jsonb->>'{ATTEMPTS_KEY}')::int ELSE 0 END) < :max_attempts
"""  # noqa: S608 -- splices only module constants; every value is bound

_KING_CV_DONE_JOB = """
      j.status = 'done'
  AND lower(sc.county) = 'king' AND upper(sc.state) = 'WA'
  AND sc.record_type = 'code_violation'
"""

# Fewest attempts first, then the longest since last tried, then the oldest row.
CANDIDATES_SQL = f"""
    SELECT r.id, r.user_id, r.property_address, r.property_city, r.property_state,
           r.property_zip, r.enrichment_data::jsonb->>'latitude' AS lat,
           r.enrichment_data::jsonb->>'longitude' AS lon,
           (CASE WHEN r.enrichment_data::jsonb->>'{ATTEMPTS_KEY}' ~ '^[0-9]{{1,6}}$'
                 THEN (r.enrichment_data::jsonb->>'{ATTEMPTS_KEY}')::int ELSE 0 END) AS attempts
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE {_KING_CV_DONE_JOB}
      AND {_ELIGIBLE_ROW}
    ORDER BY attempts ASC, coalesce(r.enrichment_data::jsonb->>'{LAST_AT_KEY}', '') ASC, r.id
    LIMIT :batch
"""  # noqa: S608 -- splices only module constants; every value is bound

# Flags move only when this write supplies a mailing address; the guard is the
# eligibility predicate as the row and its job are NOW.
_WRITE_SQL = f"""
    UPDATE results r SET
      mailing_address = CAST(:mail AS text),
      property_state = CASE WHEN CAST(:mail AS text) IS NOT NULL
                            THEN CAST(:f_property_state AS varchar) ELSE r.property_state END,
      owner_state = CASE WHEN CAST(:mail AS text) IS NOT NULL
                         THEN CAST(:f_owner_state AS varchar) ELSE r.owner_state END,
      absentee_owner = CASE WHEN CAST(:mail AS text) IS NOT NULL
                            THEN CAST(:f_absentee AS boolean) ELSE r.absentee_owner END,
      out_of_state_owner = CASE WHEN CAST(:mail AS text) IS NOT NULL
                                THEN CAST(:f_out_of_state AS boolean) ELSE r.out_of_state_owner END,
      enrichment_data = (r.enrichment_data::jsonb || CAST(:payload AS jsonb))::json
    WHERE r.id = :rid AND r.user_id = :uid
      AND {_ELIGIBLE_ROW}
      -- the lookup was made for THESE inputs; a lead edited since gets nothing
      AND r.enrichment_data::jsonb->>'latitude' IS NOT DISTINCT FROM CAST(:lat AS text)
      AND r.enrichment_data::jsonb->>'longitude' IS NOT DISTINCT FROM CAST(:lon AS text)
      AND r.property_address IS NOT DISTINCT FROM CAST(:addr AS text)
      AND r.property_zip IS NOT DISTINCT FROM CAST(:zip AS text)
      AND EXISTS (
        SELECT 1 FROM jobs j JOIN scraper_configs sc ON sc.id = j.scraper_config_id
        WHERE j.id = r.job_id AND {_KING_CV_DONE_JOB})
"""  # noqa: S608 -- splices only module constants; every value is bound

_DECISION_KEYS = ("kc_pin_status", "kc_pin", "kc_parcel_address", "kc_pin_match", "kc_pin_source")


def decision_payload(decision: dict, snapshot: str | None, now_iso: str) -> tuple[str | None, dict]:
    """(mailing, enrichment keys) for one decided row: the same keys the job writes."""
    from src.scrapers.enrichment.king_address_points import EVIDENCE_KEY

    payload = {k: decision[k] for k in (*_DECISION_KEYS, EVIDENCE_KEY) if k in decision}
    payload["kc_pin_checked_at"] = now_iso
    mail = decision.get("mailing_address")
    if mail:
        payload.update({"mailing_source": "king_rpacct", "mailing_rpacct_snapshot": snapshot})
    return mail, payload


def write_row(db, row, mail: str | None, payload: dict) -> bool:
    """One guarded UPDATE. False when the row changed under us (a no-op, not an error)."""
    from src.utils.address_intel import compute_owner_flags

    flags = compute_owner_flags(row.property_address, mail, property_city=row.property_city,
                                property_state=row.property_state, property_zip=row.property_zip)
    res = db.execute(sa_text(_WRITE_SQL), {
        "mail": mail, "rid": row.id, "uid": row.user_id, "payload": json.dumps(payload),
        "max_attempts": _MAX_ATTEMPTS, "lat": row.lat, "lon": row.lon,
        "addr": row.property_address, "zip": row.property_zip,
        "f_property_state": flags["property_state"], "f_owner_state": flags["owner_state"],
        "f_absentee": flags["absentee_owner"], "f_out_of_state": flags["out_of_state_owner"],
    })
    return bool(res.rowcount)


# Delete / extend the lock only if this tick still owns it (a tick that outlived the
# TTL must not touch the lock a newer tick now holds). Same as cv_owner_recovery.
_RELEASE_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""
_RENEW_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) end
return 0
"""


def _acquire_lock() -> tuple | str:
    """(client, token) when held, otherwise the reason to skip. Fails CLOSED."""
    import uuid

    try:
        import redis as sync_redis

        client = sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
        token = uuid.uuid4().hex
        if client.set(_LOCK_KEY, token, nx=True, ex=_LOCK_TTL_S):
            return client, token
        return "another tick is running"
    except _CELERY_TIME_LIMITS:
        raise
    except Exception as exc:  # noqa: BLE001
        return f"lock unavailable: {type(exc).__name__}"


def _renew_lock(lock: tuple) -> bool:
    client, token = lock
    try:
        return bool(client.eval(_RENEW_IF_OWNER, 1, _LOCK_KEY, token, _LOCK_TTL_S))
    except _CELERY_TIME_LIMITS:
        raise
    except Exception:  # noqa: BLE001 -- fail closed: no proof of ownership, no write
        return False


def _release_lock(lock: tuple) -> None:
    client, token = lock
    try:
        client.eval(_RELEASE_IF_OWNER, 1, _LOCK_KEY, token)
    except _CELERY_TIME_LIMITS:
        raise
    except Exception:  # noqa: BLE001, S110 -- the TTL releases it anyway
        pass


def recover_code_violation_mailing() -> dict:
    """One bounded tick. Returns a stats dict (also read by the tests)."""
    stats = {"rows": 0, "located": 0, "found": 0, "no_answer": 0, "gave_up": 0,
             "stale": 0, "errors": 0, "skipped": ""}
    if not settings.GIS_ENRICHMENT_ENABLED:
        stats["skipped"] = "GIS_ENRICHMENT_ENABLED is off"
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
    from src.scrapers.enrichment.king_parcel_locate import resolve_code_violation_mailing
    from src.scrapers.enrichment.king_rpacct import cached_extract
    from src.scrapers.enrichment.source_health import (
        KING_CV_PARCEL_LOCATE,
        is_source_available,
        mark_source_healthy,
        mark_source_unhealthy,
    )

    deadline = time.monotonic() + _TICK_BUDGET_S
    with system_sync_session() as db:
        if not is_source_available(db, KING_CV_PARCEL_LOCATE):
            stats["skipped"] = f"{KING_CV_PARCEL_LOCATE} is in cooldown"
            return stats
        rows = db.execute(sa_text(CANDIDATES_SQL),
                          {"max_attempts": _MAX_ATTEMPTS, "batch": _BATCH_ROWS}).all()
        db.rollback()  # release the read snapshot before any network I/O
        if not rows:
            return stats
        stats["rows"] = len(rows)
        by_id = {str(r.id): r for r in rows}

        def _stand_down(reason: str) -> dict:
            # Nobody is charged an attempt; the cooldown ladder keeps the next ticks off.
            mark_source_unhealthy(db, KING_CV_PARCEL_LOCATE, reason)
            db.commit()
            stats["skipped"] = f"stopped: {reason}"
            _logger.warning("Code violation mailing recovery: %s", stats["skipped"])
            return stats

        # Without a usable extract every MATCHED row would come back with no decision
        # (the resolver keeps it retryable) and be charged toward gave_up, while the
        # other outcomes made the source look healthy. Check before asking King at all.
        if cached_extract() is None:
            return _stand_down("Assessor extract unavailable")
        try:
            decisions, snapshot = resolve_code_violation_mailing(
                [(k, r.lat, r.lon, r.property_address) for k, r in by_id.items()],
                pace_s=_PACE_S, budget_s=max(30.0, deadline - time.monotonic() - 60),
                address_points=True, property_zips={k: r.property_zip for k, r in by_id.items()},
            )
        except _CELERY_TIME_LIMITS:
            raise
        except Exception as exc:  # noqa: BLE001 -- best-effort background recovery
            return _stand_down(f"lookup failed: {type(exc).__name__}: {str(exc)[:100]}")

        if not decisions and len(rows) >= _BREAKER_MIN_ROWS:
            # Not one answer for a whole batch: the parcel layer is refusing us.
            return _stand_down(f"no answer for {len(rows)} rows (parcel layer unavailable)")
        if decisions:
            mark_source_healthy(db, KING_CV_PARCEL_LOCATE)
            db.commit()

        now = datetime.now(UTC).isoformat()
        for rid, row in by_id.items():
            if not _renew_lock(lock):
                stats["skipped"] = "lock lost before writing"
                _logger.warning("Code violation mailing recovery: %s", stats["skipped"])
                break
            decision = decisions.get(rid)
            if decision is not None:
                mail, payload = decision_payload(decision, snapshot, now)
                kind = "found" if mail else "located"
            else:
                attempts = int(row.attempts) + 1
                gave_up = attempts >= _MAX_ATTEMPTS
                mail, payload = None, {ATTEMPTS_KEY: attempts, LAST_AT_KEY: now}
                if gave_up:
                    payload[OUTCOME_KEY] = "gave_up"
                kind = "gave_up" if gave_up else "no_answer"
            try:
                written = write_row(db, row, mail, payload)
                db.commit()
            except _CELERY_TIME_LIMITS:
                raise
            except Exception as exc:  # noqa: BLE001 -- one bad row must not stop the tick
                db.rollback()
                stats["errors"] += 1
                _logger.warning("Code violation mailing recovery: write failed for %s: %s",
                                rid[:8], type(exc).__name__)
                continue
            stats[kind if written else "stale"] += 1
        if stats["errors"] and stats["errors"] == stats["rows"]:
            # Every write failed: asking King again next hour would only repeat that.
            return _stand_down(f"all {stats['rows']} writes failed")

    _logger.info(
        "Code violation mailing recovery: %d lead(s); %d mailing found, %d located without "
        "mailing, %d no answer, %d gave up, %d stale, %d error%s",
        stats["rows"], stats["found"], stats["located"], stats["no_answer"], stats["gave_up"],
        stats["stale"], stats["errors"], f" ({stats['skipped']})" if stats["skipped"] else "",
    )
    return stats


try:  # pragma: no cover -- registration only
    from src.workers import app

    @app.task(name="src.workers.cv_mailing_recovery.recover_code_violation_mailing_task")
    def recover_code_violation_mailing_task() -> dict:
        """Beat entry point: see recover_code_violation_mailing."""
        return recover_code_violation_mailing()
except Exception as exc:  # pragma: no cover -- import-time safety only
    # Loud: beat would keep publishing a task nobody runs.
    _logger.error("Code violation mailing recovery task NOT registered: %s", str(exc)[:120],
                  exc_info=exc)
