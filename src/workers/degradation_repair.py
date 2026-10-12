"""Re-run the skip-trace enqueue of a DONE job whose enqueue crashed (P0-07 6b).

run_scrape_job records a `skip_trace_enqueue` degradation with
repair_status='pending' when the enqueue fails (tasks_helpers/degradation.py),
and tells the customer it will be retried. This is that retry, on the beat.

It calls the SAME enqueue the run would have (_enqueue_skip_trace_rows), so every
claim, quota, trial and duplicate-charge guard applies, and it is idempotent:
leads already queued are no longer 'not_attempted' and are skipped under the
job's claim lock. "Repaired" means that enqueue ran to completion, exactly as
the original run would have, whatever it then queued.

Concurrency: a claim leases each row by pushing next_attempt_at past the task's
hard time limit, and that lease value is the row's ownership token. Every later
write is conditional on it, so a run that outlived its lease cannot overwrite a
newer one. Rows are never deleted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from src.utils.celery_limits import reraise_time_limit
from src.utils.logger import setup_logger

_logger = setup_logger("worker.degradation_repair")

BATCH = 25
# Longer than REPAIR_TIME_LIMIT_S, so a live run never loses its rows.
LEASE = timedelta(minutes=60)
REPAIR_SOFT_TIME_LIMIT_S = 20 * 60
REPAIR_TIME_LIMIT_S = 25 * 60
MAX_ATTEMPTS = 5
BACKOFF = timedelta(minutes=15)  # doubles per failed attempt
# Waiting on something outside the job (skip trace switched off for everyone,
# no provider token, claims paused): checked again later, no attempt used.
WAIT = timedelta(hours=1)

_WAIT_OUTCOMES = {"disabled", "no_token", "unenforced"}
_SUPPRESS_OUTCOMES = {"config_off", "starter", "not_owner"}


def _claim(limit: int) -> list:
    from src.db.session import system_sync_session

    with system_sync_session() as s:
        rows = s.execute(
            text(
                "UPDATE job_degradations SET next_attempt_at = now() + :lease "
                "WHERE id IN ("
                "  SELECT id FROM job_degradations "
                "  WHERE reason = 'skip_trace_enqueue' AND repair_status = 'pending' "
                "    AND (next_attempt_at IS NULL OR next_attempt_at <= now()) "
                "  ORDER BY next_attempt_at NULLS FIRST, id LIMIT :lim "
                "  FOR UPDATE SKIP LOCKED"
                ") RETURNING id, job_id, attempts, next_attempt_at"
            ),
            {"lease": LEASE, "lim": limit},
        ).all()
        s.commit()
    return rows


def _settle(row, **values) -> bool:
    """Write ``values`` onto a claimed row, only while this run still holds it."""
    from src.db.session import system_sync_session

    sets = ", ".join(f"{k} = :{k}" for k in values)
    with system_sync_session() as s:
        n = s.execute(
            text(
                f"UPDATE job_degradations SET {sets} "  # noqa: S608 -- keys are ours
                "WHERE id = :id AND repair_status = 'pending' AND next_attempt_at = :lease"
            ),
            {**values, "id": row.id, "lease": row.next_attempt_at},
        ).rowcount
        s.commit()
    if n != 1:
        _logger.warning("Repair of degradation %s: lease lost, result not written", row.id)
    return n == 1


def _repair_one(row, r) -> str:
    """Re-run one job's enqueue. Returns the outcome to settle the row with."""
    from src.config import settings
    from src.config.constants import normalize_plan
    from src.db.models import Job, ScraperConfig
    from src.db.session import rls_sync_session, system_sync_session
    from src.workers.tasks_helpers.enrich import _enqueue_skip_trace_rows

    with system_sync_session() as s:
        found = s.execute(
            text("SELECT user_id, status FROM jobs WHERE id = :j"), {"j": row.job_id},
        ).first()
    if found is None or found.status != "done":
        return "suppressed"
    # Checked here rather than left to the helper, which would post a warning on
    # the customer's job log at every retry.
    if not settings.SKIP_TRACE_ENABLED or not settings.TRACERFY_API_TOKEN:
        return "wait"
    with rls_sync_session(str(found.user_id)) as db:
        job = db.get(Job, row.job_id)
        config = db.get(ScraperConfig, job.scraper_config_id) if job else None
        if (job is None or config is None or not config.skip_trace_enabled
                or normalize_plan(job.user.plan) == "starter"):
            return "suppressed"
        # No attempt token: the job is DONE, there is no attempt to fence.
        outcome = _enqueue_skip_trace_rows(db, job, r, row.job_id, config)
    if outcome == "done":
        return "repaired"
    if outcome in _WAIT_OUTCOMES:
        return "wait"
    if outcome in _SUPPRESS_OUTCOMES:
        return "suppressed"
    raise RuntimeError(f"unexpected enqueue outcome {outcome!r}")


def repair_degraded_skip_trace(limit: int = BATCH) -> dict:
    """One beat tick: claim due rows, re-run each enqueue, settle each row."""
    from src.workers.ops_alerts import send_ops_alert
    from src.workers.tasks_helpers.status import _redis

    stats = {"repaired": 0, "suppressed": 0, "wait": 0, "retry": 0, "gave_up": 0}
    rows = _claim(limit)
    if not rows:
        return stats
    r = _redis()
    for row in rows:
        try:
            outcome = _repair_one(row, r)
        except Exception as exc:  # noqa: BLE001 -- counted, backed off, alerted at the end
            reraise_time_limit(exc)
            attempts = row.attempts + 1
            err = type(exc).__name__[:64]
            _logger.warning("Repair of job %s failed (attempt %d): %s", row.job_id, attempts, err)
            if attempts >= MAX_ATTEMPTS:
                if _settle(row, repair_status="gave_up", attempts=attempts, last_error_type=err):
                    stats["gave_up"] += 1
                    send_ops_alert(
                        "job_degraded_repair", f"{row.job_id}:skip_trace_enqueue",
                        "Contact lookup re-queue gave up",
                        f"Job {row.job_id}: re-queuing its contact lookups failed "
                        f"{attempts} times ({err}). Re-run it by hand.",
                    )
            elif _settle(row, attempts=attempts, last_error_type=err,
                         next_attempt_at=datetime.now(UTC) + BACKOFF * 2 ** (attempts - 1)):
                stats["retry"] += 1
            continue
        if outcome == "wait":
            if _settle(row, next_attempt_at=datetime.now(UTC) + WAIT):
                stats["wait"] += 1
        elif _settle(row, repair_status=outcome):
            stats[outcome] += 1
    _logger.info("Degraded skip-trace repair: %s", stats)
    return stats
