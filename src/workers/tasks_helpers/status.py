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
from typing import Literal, NamedTuple, TypedDict, Unpack

import redis as sync_redis
from sqlalchemy import text

from src.api.quota_window import reservation_is_current_sql
from src.config import settings
from src.utils.celery_limits import reraise_time_limit
from src.utils.logger import setup_logger
from src.workers.tasks_helpers.dedup import BILLING_STAMP_RELIABLE_SINCE

_logger = setup_logger("worker.task")


class AttemptToken(NamedTuple):
    """Which attempt of a job a worker is running: what its claim stamped.

    ``started_at`` alone was the token everywhere, and it is a wall-clock value. The
    pair is unique per claim by construction, with no clock argument: a claim needs
    ``status='pending'``, and the only two writes that put a CLAIMED job back to
    pending (the watchdog re-queue and ``_retry_scrape_job``) both increment
    ``retry_count``. So two claims of one job never share a ``retry_count``, and a
    stale attempt whose timestamp happens to match a newer one's is still refused.
    ``retry_count`` never changes during an attempt: only those re-pends change it.
    """

    started_at: datetime
    retry_count: int


def _attempt_parts(expected) -> tuple:
    """(started_at, retry_count or None). A bare datetime is the legacy token form,
    kept for callers that predate AttemptToken; every production caller in
    run_scrape_job passes the token."""
    if isinstance(expected, AttemptToken):
        return expected.started_at, expected.retry_count
    return expected, None


def _attempt_clauses(expected) -> list:
    """ORM predicates pinning a write to one attempt (both halves of a token)."""
    from src.db.models import Job

    started_at, retry_count = _attempt_parts(expected)
    clauses = [Job.started_at == started_at]
    if retry_count is not None:
        clauses.append(Job.retry_count == retry_count)
    return clauses


def _attempt_sql(expected) -> tuple[str, dict]:
    """Raw-SQL twin of _attempt_clauses: a fragment over `jobs` and its binds."""
    started_at, retry_count = _attempt_parts(expected)
    if retry_count is None:
        return "started_at = :att_sa", {"att_sa": started_at}
    return ("started_at = :att_sa AND retry_count = :att_rc",
            {"att_sa": started_at, "att_rc": retry_count})

#: Is the grant this job is holding still sitting in the user's counter?
#:
#: ``rolling="false"`` because RELEASE never rolls a window — unlike settlement,
#: which advances the window in the same statement it charges and must therefore
#: refuse to net a grant against a base it has just zeroed. Here the stored
#: window is the counter's window: if it still matches the one the grant was
#: charged to, the grant is still in there and must come back; if it does not,
#: the rollover already discarded it and there is nothing to refund. With
#: ``rolling`` false the expression reduces to exactly that equality, and a
#: pre-migration-088 job (NULL window) keeps the calendar-month test it was
#: written under.
_RESERVATION_STILL_HELD = reservation_is_current_sql(
    job_window="jobs.quota_period_start",
    job_reserved_at="jobs.reserved_at",
    user_window="u.quota_period_start",
    user_records_period_start="u.records_period_start",
    rolling="false",
)

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
    """Persist a log line to the DB, then publish it to Redis Pub/Sub.

    Pass an existing ``db`` session to avoid opening a new connection per log line.

    Commit BEFORE publish (load-bearing). The live log stream subscribes and
    then reads stored lines, so a line is either committed before that read
    or published after the subscription. Publishing first let a line go out
    before a viewer subscribed yet commit after its read, and it was lost to
    that viewer until a reload. The payload id is the row id, so a line seen
    both ways is dropped by the client. A failed commit publishes nothing.
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

    r.publish(f"job_logs:{job_id}", json.dumps(payload))


_TERMINAL_STATUSES = ("done", "failed", "cancelled")


def _set_status(
    db,
    job,
    status: str,
    *,
    expected_started_at: datetime | None = None,
    commit: bool = True,
    **kwargs: Unpack[JobUpdateFields],
) -> bool:
    """Update job status and any extra fields, then commit.

    ``commit=False`` leaves the CAS UPDATE pending in the caller's transaction
    so it can be committed atomically with other writes (the done-CAS is
    committed together with billing — a crash can no longer leave a job billed
    but not done, or done but not billed). The caller MUST then commit on True
    and roll back on False; ``job`` is not refreshed in that mode.

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
        where.extend(_attempt_clauses(expected_started_at))
    rowcount = db.execute(
        _sa_update(Job).where(*where).values(status=status, **kwargs)
    ).rowcount
    if not commit:
        return rowcount == 1
    db.commit()
    db.refresh(job)
    return rowcount == 1


class JobProgressFields(TypedDict, total=False):
    """The progress columns ``_set_progress`` may write.

    The migration-099 observations are all nullable and NULL means UNOBSERVED, so
    writing a 0 to one of them is a positive statement that the answer really is
    zero. Never write 0 as a placeholder — that is the exact bug they exist to end.

    The three legacy counters below are the older, NOT NULL pair of that story and
    cannot say "unobserved" at all; they are here so that the one write which
    updates them is the guarded one. Before this they were an ORM assignment
    committed by primary key with no precondition, which let a re-queued attempt's
    late callback overwrite a replacement run's counters, or the billed
    ``record_count`` a terminal CAS had already stored (Codex round 4). Never write
    a None to any of them.
    """

    stage: str
    stage_started_at: datetime
    records_found: int
    units_done: int
    units_total: int
    progress_unit: str
    last_progress_at: datetime
    # Legacy, NOT NULL. Kept for every existing reader.
    page_current: int
    page_total: int
    record_count: int


def _set_progress(
    db,
    job,
    *,
    expected_started_at,
    commit: bool = True,
    **kwargs: Unpack[JobProgressFields],
) -> bool:
    """Write progress observations for ONE attempt. Returns whether the write landed.

    Progress is descriptive, never part of the state machine: this touches only the
    migration-099 columns and NEVER ``status``. Use ``_set_status`` for that.

    Guarded on two facts, both necessary:

      * ``status NOT IN terminal`` — a cancelled, failed or done job must not keep
        reporting that work is happening. Without this, a scraper that runs on for
        another minute after a cancel would keep advancing the counters on a
        terminal row and the Live Run page would show a cancelled run progressing.
      * ``started_at == expected_started_at`` — the attempt token. The watchdog
        re-queues a job whose worker is gone, and the replacement attempt stamps a
        fresh ``started_at``. A late callback from the OLD attempt (a scraper that
        was slow rather than dead, a thread that recovered) would otherwise
        overwrite the live attempt's counters with values from a run nobody is
        watching. Rowcount 0 means superseded: the caller must not publish either.

    ``commit=False`` leaves the UPDATE pending in the caller's transaction, which is
    the right mode at a stage boundary that is immediately followed by a
    ``_publish_log(db=db)`` — that call commits, so the stage and its log line land
    atomically and NO new commit point is introduced into the work session. That
    matters: a commit inserted mid-transaction would also commit whatever else was
    pending, and the billing writes are deliberately held uncommitted until they can
    land together with the terminal CAS.

    The mirror hazard is holding the write open too long. A pending UPDATE keeps a
    row lock on ``jobs``, and the cancel endpoint writes that same row, so leaving
    one uncommitted across a long operation (the scrape runs up to 30 minutes) would
    block the user's Cancel Run for the duration. Only ever use ``commit=False``
    where the commit is a few statements away.

    Never raises. Progress is telemetry: a failed observation must not fail a scrape
    that is otherwise working. A failure leaves the previous observation in place,
    which reads as "nothing new has been reported" — true, and the honest answer.
    """
    from sqlalchemy import update as _sa_update

    from src.db.models import Job

    if not kwargs:
        return False
    try:
        rowcount = db.execute(
            _sa_update(Job)
            .where(
                Job.id == job.id,
                Job.status.not_in(_TERMINAL_STATUSES),
                *_attempt_clauses(expected_started_at),
            )
            .values(**kwargs)
        ).rowcount
        if commit:
            db.commit()
    except Exception as exc:  # noqa: BLE001 — telemetry must never fail the run
        # ...but a Celery time limit is not a telemetry failure. It subclasses
        # Exception and lands on whatever line is running, so swallowing it here
        # would cost the task its soft-limit cleanup and let it run to the hard
        # kill. Same on the rollback: if the limit arrives during it, finishing
        # the session is the task's problem, not this function's to hide.
        reraise_time_limit(exc)
        try:
            db.rollback()
        except Exception as rollback_exc:
            reraise_time_limit(rollback_exc)
        _logger.warning(
            "Job %s: progress observation write failed (%s) — leaving the previous "
            "observation in place", job.id, ", ".join(sorted(kwargs)),
        )
        return False
    return rowcount == 1


def _set_stage(
    db, job, stage: str, *, expected_started_at, commit: bool = True,
    clear_counters: bool = True,
) -> bool:
    """Record which activity the worker has just entered. Returns whether it landed.

    Stamps ``stage_started_at`` with it, so "still connecting to King County" can be
    answered from the row instead of re-derived from log timestamps.

    Stages REPEAT and do not run in a fixed order, so this is not a step counter and
    re-entering a stage is legitimate — it re-stamps the clock, which is what the
    reassurance copy wants.

    ``clear_counters`` (default True) wipes the unit counters on the way in, and that
    default is the important part. The counters belong to the ACTIVITY that produced
    them: a scrape that finished 5 of 5 chunks leaves units_done=5, units_total=5,
    progress_unit='chunk' on the row, and without this the next stage inherits them.
    Enrichment would then be labelled "Adding property and mailing details: Part 5 of
    5" at 99% with a zero-second estimate, none of which anyone measured — a real
    number describing the wrong work, which is the exact failure this whole change
    set exists to remove (Codex).

    ``records_found`` is deliberately NOT cleared: it is a run-level total, not a
    per-activity one, and the Records tile should keep showing it after the scrape.

    Pass False only when the caller writes its own counters in the same statement.
    """
    values: dict = {"stage": stage, "stage_started_at": _now()}
    if clear_counters:
        values |= {"units_done": None, "units_total": None, "progress_unit": None}
    return _set_progress(
        db, job,
        expected_started_at=expected_started_at,
        commit=commit,
        **values,
    )


def release_quota_reservation(db, job_id: str) -> int:
    """Hand back a quota grant this job claimed but never billed. Returns freed.

    The plan cap RESERVES quota (migration 087) and charges it to
    ``users.records_used`` immediately, which is what stops two concurrent jobs
    being allocated the same remaining allowance. That means a job which dies
    between the cap and the bill is holding records the user never received —
    so every terminal-without-billing path has to release, or the reservation
    becomes a silent permanent charge.

    CONCURRENCY: the amount comes from the job UPDATE's own RETURNING, not from
    a prior SELECT. Two racing releases would both read the same
    ``reserved_count`` from an unlocked read and both refund it, subtracting the
    grant twice and eating unrelated current-period usage. Clearing
    ``reserved_at`` alone first makes the claim exclusive — the loser blocks,
    re-evaluates ``reserved_at IS NOT NULL`` against the newer row version, and
    matches nothing. ``RETURNING reserved_count`` is safe there precisely
    because that column is left untouched by this statement, so it still carries
    the original grant. (Codex)

    Guards, all necessary:
      * ``billing_applied_at IS NULL`` — a job that DID bill settled its own
        delta and owns its charge; releasing would refund a real delivery.
      * the reservation must belong to the user's CURRENT ENTITLEMENT WINDOW.
        After a rollover the counter belongs to a new window this grant was
        never added to, and subtracting from it would destroy current-window
        usage — the exact class of bug this whole area is recovering from.
        The test is now an equality on ``quota_period_start`` rather than a
        comparison of calendar MONTHS: month equality is only accidentally
        right while every window starts on the 1st, and with (say) a 20th
        anchor a grant taken on the 19th and released on the 21st would have
        been refunded out of a window that never held it. A job that reserved
        before migration 088 has a NULL ``jobs.quota_period_start`` and keeps
        the month comparison it was written under — correct for exactly those
        rows, and a set that drains. (Codex, extended)
      * ``GREATEST(0, ...)`` so a counter can never be driven negative.

    Clearing ``reserved_at`` also lets a watchdog re-run reserve afresh rather
    than reusing a grant that has already been handed back.

    A grant whose window HAS rolled is retired rather than refunded (see below):
    there is nothing left to give back, but leaving the bookkeeping set would
    keep the row in the beat sweep's result set forever.
    """
    try:
        # Exclusive claim. reserved_count is deliberately NOT written here, so
        # RETURNING still yields the original grant.
        row = db.execute(
            text(
                "UPDATE jobs SET reserved_at = NULL "
                "WHERE id = :jid "
                "  AND reserved_at IS NOT NULL "
                "  AND billing_applied_at IS NULL "
                "  AND reserved_count > 0 "
                "  AND EXISTS ("
                "    SELECT 1 FROM users u "
                "    WHERE u.id = jobs.user_id "
                "      AND " + _RESERVATION_STILL_HELD + ""
                "  ) "
                "RETURNING user_id, reserved_count"
            ),
            {"jid": job_id},
        ).fetchone()
        if row is None:
            # Nothing to refund. If the ONLY reason is that the window has
            # rolled — the grant was zeroed along with it — retire the
            # bookkeeping so the 5-minute sweep does not re-examine this job
            # for the rest of its life. Never touches the user's counter.
            retired = db.execute(
                text(
                    "UPDATE jobs SET reserved_at = NULL, reserved_count = 0 "
                    "WHERE id = :jid "
                    "  AND reserved_at IS NOT NULL "
                    "  AND billing_applied_at IS NULL "
                    "  AND reserved_count > 0"
                ),
                {"jid": job_id},
            ).rowcount
            db.commit()
            if retired:
                _logger.info(
                    "Job %s: quota reservation belonged to an entitlement "
                    "window that has already rolled — retired without refund",
                    job_id,
                )
            return 0
        user_id, amount = str(row[0]), int(row[1])
        db.execute(
            text(
                "UPDATE users SET records_used = GREATEST(0, records_used - :n) "
                "WHERE id = CAST(:uid AS uuid)"
            ),
            {"n": amount, "uid": user_id},
        )
        db.execute(
            text("UPDATE jobs SET reserved_count = 0 WHERE id = :jid"),
            {"jid": job_id},
        )
        db.commit()
        _logger.info(
            "Job %s: released %d reserved records back to the user's quota",
            job_id, amount,
        )
        return amount
    except Exception as exc:  # noqa: BLE001 — never mask the original failure
        try:
            db.rollback()
        except Exception:
            pass
        _logger.error(
            "Job %s: could not release its quota reservation: %s",
            job_id, str(exc)[:200],
        )
        return 0


def sweep_stranded_quota_reservations(limit: int = 500) -> int:
    """Release grants held by jobs that ended without ever billing.

    ``release_quota_reservation`` is called from ``_fail_job``, the post-crash
    cleanup and the cancel branch, which covers the paths that exist today. It
    cannot cover the ones that do not: a job terminalized by something that only
    writes ``jobs.status`` (an external cancel, a batch force-finalize, the
    watchdog permanently failing a stuck job) never runs any of that code, and
    its reservation stays charged to the user forever. Enumerating call sites is
    the fragile fix — the next path added would silently reintroduce it. (Codex)

    So this sweeps by STATE instead of by code path: any job already in a
    terminal status that still holds a reservation it never billed is, by
    definition, holding records the user did not receive. Runs on the beat, so
    a stranded grant is returned within minutes however the job got there.

    Returns the number of reservations released.
    """
    from src.db.session import system_sync_session

    with system_sync_session() as db:
        stranded = [
            str(row[0])
            for row in db.execute(
                text(
                    "SELECT id FROM jobs "
                    "WHERE status IN ('done', 'failed', 'cancelled') "
                    "  AND reserved_at IS NOT NULL "
                    "  AND billing_applied_at IS NULL "
                    "  AND reserved_count > 0 "
                    "ORDER BY reserved_at "
                    "LIMIT :lim"
                ),
                {"lim": limit},
            ).fetchall()
        ]
        released = 0
        for job_id in stranded:
            # Each release commits on its own, so one problem row cannot block
            # the rest, and the guards inside make a concurrent release a no-op.
            if release_quota_reservation(db, job_id):
                released += 1

    if released:
        _logger.warning(
            "Released %d stranded quota reservation(s) from terminal jobs that "
            "never billed", released,
        )
    return released


def sweep_stranded_dedup_claims(limit: int = 5000) -> int:
    """Release dedup claims held by jobs that ended without delivering anything.

    The claim is written at the dedup step, long before delivery, so a job that
    never finishes leaves claims behind that make every later run hide those
    leads as "already delivered". The in-task paths release them on the failures
    and cancellations the worker itself sees. They cannot cover a job that ended
    while no worker was running it: the worker died after claiming and the job
    was then cancelled, a redelivered task stopped at the cancelled bootstrap
    check, or the watchdog permanently failed it by writing only jobs.status
    (Codex). Same reasoning as sweep_stranded_quota_reservations: sweep by STATE,
    not by code path.

    The state is exact: failed or cancelled AND never billed. Such a job exported
    nothing a customer can reach (export_key is only written by the done-CAS) and
    charged nothing. A 'done' job, or any job that billed, is never touched. Nor
    is a job created before BILLING_STAMP_RELIABLE_SINCE: it could have charged
    without leaving a stamp, so its claims keep suppressing (Codex review round 7).

    Deliberately raises on error rather than logging: a sweep that silently could
    not delete is how 16,761 claims stranded on 2026-09-04.

    Returns the number of claims released.
    """
    from src.db.session import system_sync_session

    with system_sync_session() as db:
        result = db.execute(
            text(
                "DELETE FROM delivered_records dr "
                "WHERE dr.id IN ("
                "  SELECT d.id FROM delivered_records d "
                "  JOIN jobs j ON j.id = d.first_job_id AND j.user_id = d.user_id "
                "  WHERE j.status IN ('failed', 'cancelled') "
                "    AND j.billing_applied_at IS NULL "
                "    AND j.created_at >= :since "
                "  LIMIT :lim"
                ")"
            ),
            {"lim": limit, "since": BILLING_STAMP_RELIABLE_SINCE},
        )
        db.commit()
        released = result.rowcount or 0

    if released:
        _logger.warning(
            "Released %d dedup claim(s) held by failed or cancelled jobs that never "
            "billed", released,
        )
    return released


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
    # NOT fire, the attempt token changed (a newer attempt re-claimed, or the job was
    # re-queued for one). Suppress the
    # failure log + 'failed' SSE so a superseded attempt can't emit a false failure
    # against the live newer attempt (Codex P2). Unscoped callers are unchanged:
    # there cas_ok=False means the job was already terminal, where re-publishing the
    # failure is harmless/expected.
    if expected_started_at is not None and not cas_ok:
        _logger.info(
            "Job %s: fail suppressed: the attempt token changed; not emitting a "
            "failure event", job_id,
        )
        return cas_ok
    # Publish the failure log via a fresh session (db=None) so it
    # is not coupled to the main session's transaction state.
    # A failed job delivered nothing, so any quota it reserved must go back.
    # Doing it here rather than at each call site means every failure path is
    # covered, including ones added later. No-op when nothing was reserved.
    release_quota_reservation(db, job_id)
    _publish_log(r, job_id, "error", reason, db=None)
    r.publish(f"job_logs:{job_id}", json.dumps({"type": "failed", "error": reason}))
    _logger.error("Job %s failed: %s", job_id, reason)
    return cas_ok


def claim_job_for_attempt(db, job_id: str):
    """Atomically take ownership of a ``pending`` job for ONE attempt.

    Returns the timestamp stamped on the winning attempt, or ``None`` when the
    row was not claimable (already in flight, cancelled, or claimed by another
    worker). Compare-and-set on ``status='pending'`` is what makes a duplicate
    Celery delivery or a recovery re-enqueue a no-op instead of a second
    concurrent scrape.

    ``last_heartbeat_at`` is stamped with the SAME instant as ``started_at``, and
    that is load-bearing rather than tidy. The watchdog re-queue path resets
    status/started_at but the row can still carry the DEAD attempt's heartbeat;
    a fresh attempt's heartbeat thread does not write for up to 60s, so an
    inherited stale value would let the next watchdog tick re-queue a brand-new,
    perfectly healthy attempt, burning the retry budget until the job failed
    outright (Codex). Stamping at claim time makes an attempt immune to that
    regardless of what any re-queue path left behind.

    ``next_retry_at`` is cleared for the same class of reason. It says when a
    backed-off retry may START; once this attempt HAS started it is a past time
    describing nothing, and anything rendering a countdown from it would keep
    counting against a run that is already going. Claiming is exactly the moment it
    stops being true.

    This lives here, rather than inline in ``run_scrape_job``, so the guarantee is
    testable against the code production actually runs. A test that re-types this
    UPDATE proves only that the test's own SQL works: deleting the heartbeat stamp
    from the real claim would leave such a test green (Codex).
    """
    token = claim_attempt(db, job_id)
    return token.started_at if token is not None else None


def claim_attempt(db, job_id: str) -> AttemptToken | None:
    """The claim above, returning the whole attempt token (see AttemptToken).

    ``retry_count`` comes from the claim UPDATE's own RETURNING, so it is the value
    of the row this attempt won, never a separate read that could see a later one.
    run_scrape_job uses this; ``claim_job_for_attempt`` stays for older callers.
    """
    from sqlalchemy import update

    from src.db.models import Job

    claimed_at = _now()
    won = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "pending")
        .values(
            status="queued",
            started_at=claimed_at,
            last_heartbeat_at=claimed_at,
            next_retry_at=None,
        )
        .returning(Job.retry_count)
    ).first()
    db.commit()
    return AttemptToken(claimed_at, int(won.retry_count)) if won is not None else None


class AttemptState(NamedTuple):
    owned: bool
    status: str | None


def attempt_state(db, job_id: str, user_id, token) -> AttemptState:
    """Does ``token`` still own the job? Read from the ROW, locked.

    ``FOR UPDATE`` so the answer and the row cannot move apart before the caller
    acts on it: the caller holds the lock until it commits or rolls back. Owned =
    the token matches AND the row is not terminal: a cancel keeps the token, so a
    match alone is not ownership. A missing row (another tenant's id, or deleted)
    is never owned.
    """
    row = db.execute(
        text(
            "SELECT started_at, retry_count, status FROM jobs "
            "WHERE id = :j AND user_id = CAST(:u AS uuid) FOR UPDATE"
        ),
        {"j": str(job_id), "u": str(user_id)},
    ).first()
    if row is None:
        return AttemptState(owned=False, status=None)
    started_at, retry_count = _attempt_parts(token)
    owned = (
        row.started_at == started_at
        and (retry_count is None or row.retry_count == retry_count)
        and row.status not in _TERMINAL_STATUSES
    )
    return AttemptState(owned=owned, status=row.status)


def finalize_exit(state: AttemptState) -> Literal["terminalized", "lost_ownership"] | None:
    """What finalization does when a fenced write did not land.

    Terminal wins over the token: a job cancelled, failed or finished under this
    attempt gets today's terminal cleanup, whose releases re-check their own guards.
    Owned -> None (carry on). Anything else -> another attempt holds, or will hold,
    the job: this attempt must do nothing more.
    """
    if state.status in _TERMINAL_STATUSES:
        return "terminalized"
    if state.owned:
        return None
    return "lost_ownership"


def transient_retry_notice(
    *, retry_count: int, max_retries: int, countdown: int, published: bool
) -> str:
    """The USER-FACING line for a transient scrape failure that will be retried.

    Kept as a pure function, apart from the engineering log, so the copy customers
    actually read is greppable and unit-testable rather than buried in a 200-line
    except block. The engineering log is separate and MAY carry the exception class,
    countdown and publish outcome; this string carries none of that.

    The wording it replaced ("Transient error, retrying in ~5 min") had two problems.
    It read as though the records had failed rather than the connection, and it
    quoted a time even when the broker publish had FAILED — in which case nothing is
    scheduled and the row waits for the watchdog's stranded-retry sweep instead, so
    the promised minute count was a promise the system had not made (Codex).

    On a publish failure the copy claims NO schedule at all, not even "shortly":
    that path is picked up by the stranded-retry branch of the watchdog, which only
    looks at rows older than STUCK_STARTED_AT_FALLBACK_MINUTES, so recovery is
    bounded but can be well over an hour away. "Queued and starting shortly" would
    be as wrong as quoting the countdown (Codex).

    ``retry_count`` is the value AFTER _retry_scrape_job incremented it, so the
    attempt about to run is ``retry_count + 1`` out of ``max_retries + 1`` total.
    """
    attempt = f"attempt {retry_count + 1} of {max_retries + 1}"
    if published:
        return (
            "The county portal request could not be completed. Retrying in about "
            f"{max(1, countdown // 60)} min ({attempt})."
        )
    return (
        f"The county portal request could not be completed. This run will be retried ({attempt})."
    )


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
    starts clean and the watchdog sees a fresh un-started pending row. The
    migration-099 observations are reset with them, back to NULL rather than to 0:
    the next attempt has measured nothing yet, and 0 would assert that it had.

    ``next_retry_at`` is stamped in the SAME statement, from the SAME countdown the
    caller is about to schedule with — computed before the UPDATE precisely so the
    stored time and the scheduled time cannot drift apart by a second of jitter.
    Read it as NOT BEFORE, never as a guarantee: if the broker publish that follows
    this commit fails, the row simply sits ``pending`` until the watchdog's stranded
    retry branch picks it up, and that branch keys on ``created_at``, not on this
    column. Anything showing a countdown must be worded accordingly (Codex).
    """
    from sqlalchemy import text as _text

    attempt = int(job.retry_count or 0)
    if attempt >= max_retries:
        return None

    base = backoffs[min(attempt, len(backoffs) - 1)] if backoffs else 300
    # Jitter (0-60s) so a fleet of jobs that all fail at the same instant (e.g. a
    # portal-wide outage) don't re-hit the portal in lockstep.
    countdown = base + random.randint(0, 60)

    attempt_sql, attempt_params = _attempt_sql(started_at)
    rowcount = db.execute(
        _text(
            "UPDATE jobs SET status='pending', retry_count=retry_count+1, "
            "started_at=NULL, finished_at=NULL, error_message=NULL, "
            "page_current=0, page_total=0, record_count=0, last_heartbeat_at=NULL, "
            "stage=NULL, stage_started_at=NULL, records_found=NULL, "
            "units_done=NULL, units_total=NULL, progress_unit=NULL, "
            "last_progress_at=NULL, "
            "next_retry_at = now() + make_interval(secs => :cd) "
            f"WHERE id=:j AND {attempt_sql} AND retry_count < :mx "
            "AND status NOT IN ('done','failed','cancelled') "
            "AND billing_applied_at IS NULL"
        ),
        {"j": str(job_id), "mx": max_retries, "cd": countdown, **attempt_params},
    ).rowcount
    db.commit()
    if rowcount != 1:
        return None
    db.refresh(job)

    return countdown


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
def _heartbeat_sql(attempt_sql: str):
    return text(
        "UPDATE jobs SET last_heartbeat_at = now() "  # noqa: S608 -- splices only _attempt_sql's fixed fragment
        f"WHERE id = :j AND {attempt_sql} "
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

    Runs on ``heartbeat_sync_session`` — the ISOLATED NullPool engine, never the
    pool_size=2 work engine. Sharing that pool is what deadlocked every scrape at
    the insert phase on 2026-06-18 and got this heartbeat disabled for months. Do
    not "simplify" this back to ``system_sync_session``.
    """
    from src.db.session import heartbeat_sync_session

    try:
        with heartbeat_sync_session() as _db:
            attempt_sql, attempt_params = _attempt_sql(started_at)
            rowcount = _db.execute(
                _heartbeat_sql(attempt_sql), {"j": str(job_id), **attempt_params}
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
