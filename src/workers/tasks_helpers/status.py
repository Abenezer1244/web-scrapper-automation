"""Job status-transition + logging primitives, extracted from tasks.py.

Shared foundation the other tasks_helpers modules (and run_scrape_job) build
on: the Redis client, log publishing, the CAS status writer, the failure
transition, and the delivery download-URL builder. Moved verbatim — behavior
is byte-identical to the originals in tasks.py.
"""

import json
from datetime import UTC, datetime
from typing import TypedDict, Unpack

import redis as sync_redis

from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("worker.task")

# Delivery download links: prefer a revocable app download-token URL (honors
# logout-all + the jti blacklist, scoped to user+job) over a raw 48h R2
# presigned bearer URL sitting in an inbox. Falls back to the presigned URL
# only until settings.API_BASE_URL is configured, so delivery never breaks.
_DELIVERY_TOKEN_TTL = 172800  # 48h — matches the prior presigned URL lifetime


def _delivery_download_url(job_id: str, user_id, object_key: str, exporter) -> str:
    if settings.API_BASE_URL:
        from src.api.download_tokens import mint_download_token
        token = mint_download_token(str(user_id), job_id, ttl_seconds=_DELIVERY_TOKEN_TTL)
        return f"{settings.API_BASE_URL.rstrip('/')}/jobs/{job_id}/download?token={token}"
    return exporter.get_download_url(object_key, expires_in=_DELIVERY_TOKEN_TTL)


class JobUpdateFields(TypedDict, total=False):
    """Fields _set_status() may set on a Job ORM row alongside `status`.

    Every key is a column on src.db.models.Job; total=False because each
    callsite passes a different subset (e.g. just `started_at` on
    transition into "queued", but `finished_at` + `record_count` +
    `export_key` on transition into "done"). Using TypedDict + Unpack
    means a typo like `started=` (instead of `started_at`) is now a
    static type error rather than a silent setattr no-op.
    """

    started_at: datetime
    finished_at: datetime
    record_count: int
    page_current: int
    page_total: int
    error_message: str
    export_key: str


def _now() -> datetime:
    return datetime.now(UTC)


def _redis() -> sync_redis.Redis:
    # M1: go through redis_kwargs() so this client also verifies the broker
    # TLS cert (ssl_cert_reqs + CA bundle), not just decode_responses.
    return sync_redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())


def _publish_log(r: sync_redis.Redis, job_id: str, level: str, message: str, db=None) -> None:
    """Publish a log line to Redis Pub/Sub and persist it to the DB.

    Pass an existing ``db`` session to avoid opening a new connection per log line.
    """
    import uuid

    from src.db.models import JobLog

    payload = {
        "id": str(uuid.uuid4()),
        "level": level,
        "message": message,
        "created_at": _now().isoformat(),
        "type": "log",
    }
    r.publish(f"job_logs:{job_id}", json.dumps(payload))

    # Persist to DB for SSE replay
    if db is not None:
        db.add(JobLog(
            id=payload["id"],
            job_id=job_id,
            level=level,
            message=message,
        ))
        db.commit()
    else:
        # Fallback path — no session was passed in, so we open a
        # system-level session. JobLog writes are keyed on job_id and
        # the caller has already verified ownership upstream; the
        # table's RLS policy (job_logs_via_job) filters reads via a
        # subquery on jobs.user_id, not writes.
        from src.db.session import system_sync_session
        with system_sync_session() as _db:
            _db.add(JobLog(
                id=payload["id"],
                job_id=job_id,
                level=level,
                message=message,
            ))
            _db.commit()


_TERMINAL_STATUSES = ("done", "failed", "cancelled")


def _set_status(db, job, status: str, **kwargs: Unpack[JobUpdateFields]) -> bool:
    """Update job status and any extra fields, then commit.

    Terminal-write guard (Track A, Codex P2): the write is a CAS that only
    touches a row still in a NON-terminal status. If the row was terminalized
    externally — e.g. batch force-finalize cancelled a child that was still
    mid-scrape — the UPDATE is a no-op and this returns False so the caller
    stops instead of resurrecting a cancelled/failed/done job (and then
    billing/emailing for it). The ORM object is refreshed either way, so
    `job.status` reflects the DB after the call.

    `kwargs` keys are constrained by the JobUpdateFields TypedDict so a
    typo like `started=...` (instead of `started_at=...`) fails type
    checking instead of silently doing nothing.
    """
    from sqlalchemy import update as _sa_update

    from src.db.models import Job

    rowcount = db.execute(
        _sa_update(Job)
        .where(Job.id == job.id, Job.status.not_in(_TERMINAL_STATUSES))
        .values(status=status, **kwargs)
    ).rowcount
    db.commit()
    db.refresh(job)
    return rowcount == 1


def _fail_job(db, job, r, job_id: str, reason: str) -> None:
    """Transition job to FAILED with a human-readable error message.

    H3 (full-SaaS review): the previous implementation rolled back
    the main session before calling _set_status and _publish_log.
    If any JobLog rows had been queued via _publish_log(db=db) but
    not yet committed in the same transaction, the rollback
    destroyed them — losing the failure context for the user. The
    final failure log line also ran against a session that had
    just been rolled back, which is a fragile code path.

    Now:
      1. The state transition (jobs.status = 'failed') goes through
         the main session so _set_status can commit + refresh
         normally. If the main session was in a failed transaction
         state from an upstream exception, we recover it once with
         rollback() before the update.
      2. The failure-log _publish_log call passes db=None so it
         opens a fresh system_sync_session for the INSERT. This
         guarantees the failure message lands in job_logs even if
         the main session is misbehaving.
    """
    try:
        db.rollback()  # Recover from any pending failed transaction
    except Exception:
        pass
    try:
        _set_status(db, job, "failed", finished_at=_now(), error_message=reason)
    except Exception as exc:
        _logger.error(
            "Job %s: _set_status failed during _fail_job: %s",
            job_id, str(exc)[:200],
        )
    # Publish the failure log via a fresh session (db=None) so it
    # is not coupled to the main session's transaction state.
    _publish_log(r, job_id, "error", reason, db=None)
    r.publish(f"job_logs:{job_id}", json.dumps({"type": "failed", "error": reason}))
    _logger.error("Job %s failed: %s", job_id, reason)
