"""Background naming of Tacoma code-violation owners a job did not reach.

WHY THIS EXISTS
---------------
The live Pierce code-violation owner pass (tasks_helpers/enrich.py) runs inside a
bounded budget: one ATIP page view per parcel at 2 s pacing. A job with more parcels
than the budget, a lease held by another pass, or a portal hiccup leaves delivered
leads with no owner and no owner_status. This sweep finishes them.

WHAT THIS IS NOT
----------------
An OWNER-NAME fill and nothing else, on the same boundary as owner_recovery.py:
  * It never creates a Job and never touches quota, billing, `reserved_count` or
    `records_used`.
  * It never enqueues a skip trace and never touches `skip_trace_status`, phone,
    email, mailing, parcel_id or dedup.
  * It only fills a `party_name` that is still blank, on a delivered Tacoma
    code-violation row of a terminal job whose parcel_id is unchanged. Each
    condition is re-checked in the UPDATE itself.

Owner decision 2026-09-14: ATIP taxpayer names are stored for code-violation owner
naming only. `PIERCE_CV_OWNER_ENABLED` (default off) stops this sweep before any
request, and the lookup itself holds the Pierce owner lease (one pass at a time,
fail closed) and honours the source cooldown.
"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from sqlalchemy import text as sa_text

from src.config import settings
from src.scrapers.enrichment.pierce_atip_owner import OWNER_ROW_GUARD
from src.utils.logger import setup_logger

_logger = setup_logger("worker.pierce_cv_owner_recovery")

ATTEMPTS_KEY = "owner_recovery_attempts"
LAST_AT_KEY = "owner_recovery_last_at"

# A transient failure is retried this many times across ticks, then the lead is
# settled as gave_up so a permanently failing parcel cannot hold its place.
_MAX_ATTEMPTS = 5
# Parcels per tick: a page view is ~10 s plus 2 s pacing, so 20 parcels fit the budget.
_BATCH_PARCELS = 20
_TICK_BUDGET_S = 300.0

_LOCK_KEY = "bl:pierce_cv_owner_recovery:lock"
_LOCK_TTL_S = 900

_ELIGIBLE_ROW = """
      r.is_duplicate = false
  AND r.enrichment_data::jsonb->>'delivery_excluded_reason' IS NULL
  AND jsonb_typeof(r.enrichment_data::jsonb) = 'object'
  AND r.enrichment_data::jsonb->>'source' = 'tacoma_code_violations'
  AND (r.party_name IS NULL OR btrim(r.party_name) = '')
  AND btrim(r.parcel_id) ~ '^[0-9]{10}$'
  AND NOT (r.enrichment_data::jsonb ? 'owner_status')
  AND NOT (r.enrichment_data::jsonb ? 'owner_source')
  AND (CASE WHEN r.enrichment_data::jsonb->>'owner_recovery_attempts' ~ '^[0-9]{1,6}$'
            THEN (r.enrichment_data::jsonb->>'owner_recovery_attempts')::int ELSE 0 END)
      < :max_attempts
"""

_JOB_SCOPE = """
      j.status = 'done'
  AND lower(sc.county) = 'pierce' AND upper(sc.state) = 'WA'
  AND sc.record_type = 'code_violation'
"""

_CANDIDATE_PARCELS_SQL = f"""
    SELECT btrim(r.parcel_id) AS parcel_id,
           min(CASE WHEN r.enrichment_data::jsonb->>'owner_recovery_attempts' ~ '^[0-9]{{1,6}}$'
                    THEN (r.enrichment_data::jsonb->>'owner_recovery_attempts')::int ELSE 0 END)
             AS attempts,
           min(coalesce(r.enrichment_data::jsonb->>'owner_recovery_last_at', '')) AS last_at
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE {_JOB_SCOPE} AND {_ELIGIBLE_ROW}
    GROUP BY btrim(r.parcel_id)
    ORDER BY attempts ASC, last_at ASC, parcel_id ASC
    LIMIT :batch
"""  # noqa: S608 -- splices only module constants; every value is bound

_CANDIDATE_ROWS_SQL = f"""
    SELECT r.id, r.user_id, r.job_id, btrim(r.parcel_id) AS parcel_id, r.property_address,
           r.enrichment_data::jsonb AS ed
    FROM results r
    JOIN jobs j ON j.id = r.job_id
    JOIN scraper_configs sc ON sc.id = j.scraper_config_id
    WHERE {_JOB_SCOPE} AND {_ELIGIBLE_ROW}
      AND btrim(r.parcel_id) = ANY(:parcels)
    ORDER BY r.id
"""  # noqa: S608 -- splices only module constants; every value is bound

# The shared owner guard (same row, parcel and address as the decision, still unnamed
# and undecided) plus this sweep's own delivery and job scope, all re-checked at write.
_WRITE_SQL = f"""
    UPDATE results r SET
      party_name = COALESCE(CAST(:owner AS varchar), r.party_name),
      enrichment_data = (r.enrichment_data::jsonb || CAST(:payload AS jsonb))::json
    WHERE {OWNER_ROW_GUARD}
      AND {_ELIGIBLE_ROW}
      AND EXISTS (
        SELECT 1 FROM jobs j JOIN scraper_configs sc ON sc.id = j.scraper_config_id
        WHERE j.id = r.job_id AND {_JOB_SCOPE})
"""  # noqa: S608 -- splices only module constants; every value is bound

_RELEASE_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _acquire_lock() -> tuple | str:
    """Single-flight lock: (client, token) when held, otherwise the reason to skip. Fails CLOSED."""
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


def recover_pierce_cv_owners() -> dict:
    """One bounded tick. Returns a stats dict (also read by the tests)."""
    stats = {"parcels": 0, "rows": 0, "transient": 0, "gave_up": 0, "unreached": 0,
             "stale": 0, "errors": 0, "skipped": ""}
    if not settings.PIERCE_CV_OWNER_ENABLED:
        stats["skipped"] = "PIERCE_CV_OWNER_ENABLED is off"
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
    from src.scrapers.enrichment.pierce_atip_owner import (
        decide,
        lookup_parcels,
        owner_payload,
    )

    deadline = time.monotonic() + _TICK_BUDGET_S
    with system_sync_session() as db:
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
        parcels = [p for p in parcels if p in by_parcel]
        if not parcels:
            return stats
        stats["parcels"], stats["rows"] = len(parcels), len(rows)

        l_stats: dict = {}
        # Never floored: time already spent on the queries comes out of the tick budget.
        fetched = lookup_parcels(parcels, budget_s=deadline - time.monotonic() - 15,
                                 stats=l_stats)
        stats["lookup_outcome"] = l_stats.get("outcome")
        transient = set(l_stats.get("transient", []))
        checked_at = _now_iso()
        for pid in parcels:
            f = fetched.get(pid)
            for row in by_parcel[pid]:
                if f is not None:
                    d = decide(pid, f.rows, row.property_address, source=row.ed.get("source"))
                    payload = owner_payload(pid, d, checked_at)
                    payload[LAST_AT_KEY] = checked_at
                    label = d.status
                    owner = d.name
                elif pid in transient:
                    attempts = _attempts(row.ed) + 1
                    payload = {ATTEMPTS_KEY: attempts, LAST_AT_KEY: checked_at}
                    label, owner = "transient", None
                    if attempts >= _MAX_ATTEMPTS:
                        payload["owner_status"] = "gave_up"
                        payload["owner_checked_at"] = checked_at
                        label = "gave_up"
                else:
                    stats["unreached"] += 1  # not asked: rotate, charge nothing
                    continue
                key = _write(db, row, payload, owner, label)
                stats[key] = stats.get(key, 0) + 1
    _logger.info("Pierce CV owner recovery: %s", json.dumps(stats))
    return stats


def _attempts(ed) -> int:
    try:
        return int((ed or {}).get(ATTEMPTS_KEY) or 0)
    except (TypeError, ValueError):
        return 0


def _write(db, row, payload: dict, owner: str | None, label: str) -> str:
    """One guarded UPDATE. Returns the stats key: `label` when written, `stale` when the
    row changed since selection (nothing written), `errors` when the write failed."""
    try:
        result = db.execute(sa_text(_WRITE_SQL), {
            "rid": row.id, "uid": row.user_id, "jid": row.job_id, "pid": row.parcel_id,
            "address": row.property_address, "owner": owner,
            "payload": json.dumps(payload), "max_attempts": _MAX_ATTEMPTS,
        })
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        _logger.warning("Pierce CV owner recovery: write failed for row %s (%s): %s",
                        str(row.id)[:8], label, type(exc).__name__)
        return "errors"
    return label if result.rowcount else "stale"


try:  # pragma: no cover -- registration only
    from src.workers import app

    # Limits above the 300 s tick budget (every lookup step is itself timeout-bounded),
    # far below the app-wide 55 min, so a wedged browser cannot hold a worker for an hour.
    @app.task(name="src.workers.pierce_cv_owner_recovery.recover_pierce_cv_owners_task",
              soft_time_limit=540, time_limit=600)
    def recover_pierce_cv_owners_task() -> dict:
        """Beat entry point: see recover_pierce_cv_owners."""
        return recover_pierce_cv_owners()
except Exception as exc:  # pragma: no cover -- import-time safety only
    # Loud: beat would keep publishing a task nobody runs.
    _logger.error("Pierce CV owner recovery task NOT registered: %s", str(exc)[:120])
