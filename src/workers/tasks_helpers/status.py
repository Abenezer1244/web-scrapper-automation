"""Job status-transition + logging primitives, extracted from tasks.py.

Shared foundation the other tasks_helpers modules (and run_scrape_job) build
on: the Redis client, log publishing, the CAS status writer, the failure
transition, and the delivery download-URL builder. Moved verbatim — behavior
is byte-identical to the originals in tasks.py.
"""

import json
import random
import threading
import time
from datetime import UTC, datetime
from typing import TypedDict, Unpack

import redis as sync_redis
from sqlalchemy import text

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
    # API_BASE_URL unset: the only remaining path is the raw R2/S3 presign, which
    # 401s in production (the R2 S3 presign keypair is broken — see BACKLOG §4).
    # Fail the delivery LOUDLY instead of emailing the customer a dead link they
    # only notice days later: a failed job is visible to ops (M6 alerting) and
    # retryable, whereas a silent 401 link looks successful internally. This
    # guard is naturally worker-scoped (only the worker mints delivery links).
    if settings.ENVIRONMENT.strip().lower() == "production":
        raise RuntimeError(
            "API_BASE_URL is required in production to mint delivery download "
            "links; the R2/S3 presign fallback is broken in prod (401). Set "
            "API_BASE_URL on the Railway worker service."
        )
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


def _set_status(
    db,
    job,
    status: str,
    *,
    expected_started_at: datetime | None = None,
    **kwargs: Unpack[JobUpdateFields],
) -> bool:
    """Update job status and any extra fields, then commit.

    Terminal-write guard (Track A, Codex P2): the write is a CAS that only
    touches a row still in a NON-terminal status. If the row was terminalized
    externally — e.g. batch force-finalize cancelled a child that was still
    mid-scrape — the UPDATE is a no-op and this returns False so the caller
    stops instead of resurrecting a cancelled/failed/done job (and then
    billing/emailing for it). The ORM object is refreshed either way, so
    `job.status` reflects the DB after the call.

    ``expected_started_at`` (optional) makes the CAS ATTEMPT-scoped: the UPDATE
    also requires ``started_at`` to still equal this attempt's value. A caller
    that only wants to terminalize the attempt it is running (e.g. the scrape
    failure path) passes it so a STALE attempt can't fail a NEWER live attempt
    that already re-claimed the job (watchdog re-queue / redelivery). Omitted =
    status-only guard (unchanged behaviour for every existing caller).

    `kwargs` keys are constrained by the JobUpdateFields TypedDict so a
    typo like `started=...` (instead of `started_at=...`) fails type
    checking instead of silently doing nothing.
    """
    from sqlalchemy import update as _sa_update

    from src.db.models import Job

    where = [Job.id == job.id, Job.status.not_in(_TERMINAL_STATUSES)]
    if expected_started_at is not None:
        where.append(Job.started_at == expected_started_at)
    rowcount = db.execute(
        _sa_update(Job).where(*where).values(status=status, **kwargs)
    ).rowcount
    db.commit()
    db.refresh(job)
    return rowcount == 1


def _fail_job(db, job, r, job_id: str, reason: str, expected_started_at=None) -> bool:
    """Transition job to FAILED with a human-readable error message.

    ``expected_started_at`` (optional) forwards to _set_status to make the FAILED
    transition ATTEMPT-scoped — pass this attempt's started_at from any long-running
    phase (e.g. the scrape failure path) so a stale/superseded attempt cannot fail a
    newer live attempt that already re-claimed the job. Omitted = status-only CAS
    (unchanged for existing callers).

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

    Returns the CAS boolean from _set_status so callers can gate
    notification emit on whether the transition actually succeeded
    (False means the job was already terminal — no-op, no emit).
    """
    try:
        db.rollback()  # Recover from any pending failed transaction
    except Exception:
        pass
    cas_ok = False
    try:
        cas_ok = _set_status(
            db, job, "failed",
            expected_started_at=expected_started_at,
            finished_at=_now(), error_message=reason,
        )
    except Exception as exc:
        _logger.error(
            "Job %s: _set_status failed during _fail_job: %s",
            job_id, str(exc)[:200],
        )
    # Attempt-scoped no-op: when expected_started_at was supplied and the CAS did
    # NOT fire, a NEWER attempt re-claimed this job (started_at moved). Suppress the
    # failure log + 'failed' SSE so a superseded attempt can't emit a false failure
    # against the live newer attempt (Codex P2). Unscoped callers are unchanged:
    # there cas_ok=False means the job was already terminal, where re-publishing the
    # failure is harmless/expected.
    if expected_started_at is not None and not cas_ok:
        _logger.info(
            "Job %s: fail suppressed — attempt superseded by a newer one "
            "(started_at moved); not emitting a failure event", job_id,
        )
        return cas_ok
    # Publish the failure log via a fresh session (db=None) so it
    # is not coupled to the main session's transaction state.
    _publish_log(r, job_id, "error", reason, db=None)
    r.publish(f"job_logs:{job_id}", json.dumps({"type": "failed", "error": reason}))
    _logger.error("Job %s failed: %s", job_id, reason)
    return cas_ok


def _retry_scrape_job(
    db,
    job,
    job_id: str,
    started_at,
    *,
    max_retries: int,
    backoffs: tuple[int, ...],
) -> int | None:
    """Attempt-scoped CAS re-queue of a job whose scrape phase raised a TRANSIENT
    error. Resets the row to a fresh ``pending`` attempt so the worker's own claim
    CAS (``pending``->``queued``) can re-run it, and returns the backoff COUNTDOWN
    in seconds for the caller to pass to ``apply_async``. Returns ``None`` when
    retries are exhausted OR the CAS no-ops — the caller then fails the job.

    The UPDATE is guarded on (id, started_at, retry_count < max_retries,
    non-terminal status, ``billing_applied_at IS NULL``) so it can only ever
    re-queue THIS attempt and NEVER a job that:
      - was terminalized externally (batch force-finalize / cancel),
      - was already re-claimed by a newer attempt (started_at moved), or
      - already reached billing (``billing_applied_at`` set) — the double-bill
        guard. Billing runs only AFTER a successful scrape, so at a scrape-phase
        failure this is always NULL; the predicate is belt-and-suspenders.

    Resets the progress + liveness columns too (``started_at``/``finished_at``/
    ``error_message``/page counters/``last_heartbeat_at``) so the retried attempt
    starts clean and the watchdog sees a fresh un-started pending row.
    """
    from sqlalchemy import text as _text

    attempt = int(job.retry_count or 0)
    if attempt >= max_retries:
        return None

    rowcount = db.execute(
        _text(
            "UPDATE jobs SET status='pending', retry_count=retry_count+1, "
            "started_at=NULL, finished_at=NULL, error_message=NULL, "
            "page_current=0, page_total=0, record_count=0, last_heartbeat_at=NULL "
            "WHERE id=:j AND started_at=:sa AND retry_count < :mx "
            "AND status NOT IN ('done','failed','cancelled') "
            "AND billing_applied_at IS NULL"
        ),
        {"j": str(job_id), "sa": started_at, "mx": max_retries},
    ).rowcount
    db.commit()
    if rowcount != 1:
        return None
    db.refresh(job)

    base = backoffs[min(attempt, len(backoffs) - 1)] if backoffs else 300
    # Jitter (0-60s) so a fleet of jobs that all fail at the same instant (e.g. a
    # portal-wide outage) don't re-hit the portal in lockstep.
    return base + random.randint(0, 60)


# ── Liveness heartbeat (watchdog input) ──────────────────────────────────────
# Updates jobs.last_heartbeat_at in its OWN short system transaction — NEVER the
# caller's work session — so proving liveness can't commit partial scrape/enrich
# state. The CAS excludes terminal statuses so a heartbeat can never resurrect a
# done/failed/cancelled job, and the rowcount tells the heartbeat thread when the
# job has gone terminal (so it can self-reap).
# Attempt-scoped: the WHERE pins started_at to the attempt that started THIS
# thread. Each claim stamps a fresh started_at, so a thread left over from a
# prior attempt (e.g. one whose writes failed long enough for the watchdog to
# re-queue the job, then recovered) updates 0 rows against the re-claimed attempt
# and self-reaps — it can't refresh and mask a dead new attempt (Codex). The
# terminal-status exclusion also means a heartbeat never resurrects a done/
# failed/cancelled job.
_HEARTBEAT_SQL = text(
    "UPDATE jobs SET last_heartbeat_at = now() "
    "WHERE id = :j AND started_at = :sa "
    "AND status NOT IN ('done', 'failed', 'cancelled')"
)

# _write_heartbeat result codes.
_HB_ALIVE = 1     # row updated — job still active and this attempt still owns it
_HB_TERMINAL = 0  # rowcount 0 — job terminal/gone OR re-claimed by a newer attempt
_HB_ERROR = -1    # write failed — treat as alive (don't strand a healthy job)


def _write_heartbeat(job_id: str, started_at) -> int:
    """Best-effort liveness ping for one job ATTEMPT. Returns an _HB_* code.

    ``started_at`` scopes the write to the attempt that owns this thread; a
    rowcount of 0 means the job is terminal, gone, or has been re-claimed by a
    newer attempt — in every case this thread should stop. Never raises: liveness
    is best-effort and must not fail a job. A write error returns _HB_ERROR (not
    _HB_TERMINAL) so a single bad commit doesn't stop the thread and strand a
    healthy job — the thread counts consecutive errors instead.
    """
    from src.db.session import system_sync_session

    try:
        with system_sync_session() as _db:
            rowcount = _db.execute(
                _HEARTBEAT_SQL, {"j": str(job_id), "sa": started_at}
            ).rowcount
            _db.commit()
            return _HB_ALIVE if rowcount == 1 else _HB_TERMINAL
    except Exception:  # noqa: BLE001 — liveness is best-effort; never fail a job on it
        _logger.debug("heartbeat write failed for job %s", job_id, exc_info=True)
        return _HB_ERROR


class HeartbeatThread:
    """Background liveness pinger for a running scrape job.

    Writes jobs.last_heartbeat_at every ``interval_s`` from a daemon thread so a
    long-but-healthy scrape/enrich proves it is alive and the watchdog does not
    falsely re-queue it.

    Lifecycle — stop() (called from __exit__) is the PRIMARY shutdown, so use it
    as a context manager wrapping the task body:

        with HeartbeatThread(job_id) as hb, rls_sync_session(uid) as db:
            ... claim ...
            hb.start()        # start only once THIS worker owns the job
            ... work ...
        # __exit__ -> stop() fires on normal exit, return, OR exception

    Two backstops cover the case where stop() somehow doesn't run:
      1. Self-reap: the heartbeat CAS returns rowcount 0 once the job is terminal,
         so the thread stops within one interval.
      2. ``_MAX_LIFETIME_S`` hard cap: a daemon thread also dies with the worker
         process (Celery's hard time_limit kill — the one moment a re-queue SHOULD
         happen), and the cap sits just above the 65min hard limit so an orphaned
         thread can never pin a non-terminal job "alive" indefinitely.
    """

    __slots__ = ("_job_id", "_interval", "_stop", "_thread", "_started_at")

    _MAX_LIFETIME_S = 75 * 60  # backstop > 65min Celery hard limit; reaps orphans only
    # Log once consecutive write failures cross this threshold. Sustained failure
    # lets last_heartbeat_at go stale; the watchdog may then re-queue a still-live
    # worker — non-duplicating once migration 062 ships, but worth surfacing.
    _FAIL_WARN_AT = 5

    def __init__(self, job_id: str, interval_s: float = 60.0) -> None:
        self._job_id = str(job_id)
        self._interval = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = None

    def _run(self) -> None:
        deadline = time.monotonic() + self._MAX_LIFETIME_S
        consecutive_failures = 0
        while not self._stop.is_set() and time.monotonic() < deadline:
            code = _write_heartbeat(self._job_id, self._started_at)
            if code == _HB_TERMINAL:
                return  # job terminal/gone — self-reap
            if code == _HB_ERROR:
                consecutive_failures += 1
                if (
                    consecutive_failures == self._FAIL_WARN_AT
                    or consecutive_failures % 30 == 0
                ):
                    # Once a heartbeat has succeeded at least once, last_heartbeat_at
                    # is non-NULL and the watchdog uses the 15-min STALE-heartbeat
                    # path (not the started_at fallback) — so sustained write
                    # failures on a still-live worker CAN make the watchdog re-queue
                    # it. That re-queue is non-duplicating once the idempotent-insert
                    # work ships (migration 062), but it is worth surfacing to ops.
                    _logger.warning(
                        "heartbeat for job %s failed %d× consecutively — "
                        "last_heartbeat_at is going stale; the watchdog may re-queue "
                        "this job even though the worker is alive",
                        self._job_id, consecutive_failures,
                    )
            else:
                consecutive_failures = 0
            self._stop.wait(self._interval)

    def start(self, started_at) -> "HeartbeatThread":
        """Start beating for the attempt identified by ``started_at``.

        ``started_at`` must be the value the claim UPDATE stamped on this job
        (job.started_at after the claim). It scopes every heartbeat write to this
        attempt so a thread from a superseded attempt can't refresh the row.
        """
        if self._thread is not None:
            # Idempotent: a second start() would orphan the first thread (only the
            # latest is tracked by stop()). Never expected on the one code path,
            # but the helper is reused — don't leak.
            return self
        self._started_at = started_at
        self._thread = threading.Thread(
            target=self._run, name=f"heartbeat-{self._job_id[:8]}", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)

    def __enter__(self) -> "HeartbeatThread":
        # Does NOT start the thread — the caller starts it only after claiming the
        # job (see class docstring). __exit__ stops it regardless.
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()
