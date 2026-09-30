"""Piece 2: batch-scrape fan-out worker (Phase 2A.2; run-scoped since 2B).

`dispatch_batch_run` is enqueued with a **BatchRun id** (the durable 'pending'
intent created by POST /batches — or, in 2B, by the scheduler when a schedule
fires). It materializes that run: creates one Job per child config and flips
pending->running — mirroring the scheduler's job-dispatch (sync session, commit
BEFORE .delay so a worker can't pick up an uncommitted job). The completion
barrier (Phase 2A.3) takes over once the children settle.

2B made runs PLURAL per batch (migration 052), so the task contract is the RUN
id, not the batch id — selecting "the run for a batch" is ambiguous once history
exists (Codex P1). A transitional path still accepts a batch id (pre-deploy
queued payloads): the ref is resolved as a run PK first, then as a batch whose
ACTIVE run (or new pending run) is dispatched.

Idempotent: the run row is locked FOR UPDATE; only a 'pending' run materializes,
'running' re-enqueues lost children, terminal runs no-op.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.config.constants import ACTIVE_STATUSES
from src.db.models import BatchRun, Job, ScraperBatch, ScraperConfig, User
from src.db.session import system_sync_session
from src.utils.logger import setup_logger
from src.workers import app
from src.workers.tasks import run_scrape_job

_logger = setup_logger("worker.batch")

_ACTIVE_RUN_STATUSES = ("pending", "running")

# Migration 104's partial unique index: one active job per scraper config.
_ONE_ACTIVE_JOB_INDEX = "uq_jobs_one_active_per_config"


def _is_one_active_job_violation(exc: IntegrityError) -> bool:
    """True only for a unique violation on migration 104's index. psycopg2 (the
    worker's driver) exposes SQLSTATE as .pgcode and the name on .diag."""
    orig = getattr(exc, "orig", None)
    if getattr(orig, "pgcode", None) != "23505":
        return False
    diag = getattr(orig, "diag", None)
    return getattr(diag, "constraint_name", None) == _ONE_ACTIVE_JOB_INDEX


def _pending_child_ids(db, run: "BatchRun") -> list[str]:
    """Child job ids of `run` still in 'pending' — i.e. created but not yet picked
    up. Used to RECOVER the commit-before-delay crash window: re-enqueuing these is
    safe because run_scrape_job claims a job with an ATOMIC compare-and-set
    (UPDATE ... WHERE status='pending'), so a job already in flight is no longer
    'pending' and a re-enqueued duplicate is a no-op (return on rowcount 0)."""
    if not run.child_job_ids:
        return []
    rows = db.execute(
        select(Job.id).where(Job.id.in_(run.child_job_ids), Job.status == "pending")
    ).scalars().all()
    return [str(x) for x in rows]


def _resolve_run(db, ref: str) -> "BatchRun | None":
    """Resolve the task ref to a locked BatchRun.

    New contract: ref IS a BatchRun id. Transitional (pre-2B queued payloads):
    ref is a ScraperBatch id — resolve its ACTIVE run; if none exists (old-API
    batch that never got its durable intent), create one 'pending'. The partial
    unique index uq_batch_runs_one_active makes that create at-most-once under a
    race (IntegrityError loser re-selects the winner's row).
    """
    run = db.execute(
        select(BatchRun).where(BatchRun.id == ref).with_for_update()
    ).scalar_one_or_none()
    if run is not None:
        return run

    batch = db.get(ScraperBatch, ref)
    if batch is None:
        return None
    run = db.execute(
        select(BatchRun)
        .where(BatchRun.batch_id == batch.id, BatchRun.status.in_(_ACTIVE_RUN_STATUSES))
        .with_for_update()
    ).scalar_one_or_none()
    if run is not None:
        return run
    db.add(
        BatchRun(
            id=str(uuid.uuid4()),
            batch_id=batch.id,
            user_id=batch.user_id,
            status="pending",
            child_job_ids=[],
        )
    )
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
    return db.execute(
        select(BatchRun)
        .where(BatchRun.batch_id == batch.id, BatchRun.status.in_(_ACTIVE_RUN_STATUSES))
        .with_for_update()
    ).scalar_one_or_none()


@app.task(name="src.workers.batch_tasks.dispatch_batch_run")
def dispatch_batch_run(run_id: str) -> None:
    from src.config.constants import SCRAPE_QUEUE_DEFAULT, scrape_queue_for_plan

    enqueued: list[str] = []
    # Resolved inside the session from the batch owner's plan; the publish below
    # happens after the session closes, so it has to be captured here.
    queue: str = SCRAPE_QUEUE_DEFAULT
    with system_sync_session() as db:
        # Lock the run row FOR UPDATE so concurrent dispatches serialize: exactly
        # one transitions pending->running + creates jobs; the rest see 'running'
        # and fall to RECOVERY.
        run = _resolve_run(db, run_id)
        if run is None:
            _logger.warning("dispatch_batch_run: no run/batch for ref %s", run_id)
            return
        batch = db.get(ScraperBatch, run.batch_id)
        if batch is None:
            _logger.warning("dispatch_batch_run: batch %s not found", run.batch_id)
            return

        # One queue decision for the whole fan-out: every child belongs to the
        # batch owner. Read here, before either branch, so the RECOVERY branch
        # (which never loads `user`) routes identically to the first dispatch.
        _owner_plan = db.execute(
            select(User.plan).where(User.id == batch.user_id)
        ).scalar_one_or_none()
        queue = scrape_queue_for_plan(_owner_plan)

        if run.status == "pending":
            # MATERIALIZE the pending intent: create child jobs + flip to running
            # (or a terminal state), all in this one locked transaction.
            # dispatch_attempts is owned by batch_recovery_sweep (the bound on
            # re-dispatch), not bumped here — this is the normal first execution.
            # Quota gate at dispatch — matches the scheduler's enforcement boundary.
            # records_used can change between create-time preflight and now (Codex);
            # re-check. -1 = unlimited. Over limit => a terminal run, no jobs.
            user = db.get(User, batch.user_id)
            from src.api.quota import quota_block_reason

            _blocked = quota_block_reason(user) if user else None
            over_limit = bool(_blocked)
            if over_limit:
                run.status = "failed"
                # The reason is recorded verbatim so the run explains itself:
                # "record limit reached" and "payment failed" send the customer
                # to completely different remedies.
                run.failed_children = [{"reason": _blocked}]
                run.completed_at = datetime.now(UTC)
                db.commit()
            else:
                from src.api.entitlements import (
                    PAUSED_REASON_ENTITLEMENT,
                    ConfigRow,
                    config_run_violation,
                    current_batch_child_clause,
                    should_block_run,
                )
                configs = db.execute(
                    # Owner-scoped (defense-in-depth on top of the composite FK).
                    # Children the user deleted are excluded HERE, in SQL, so an
                    # all-deleted batch takes the zero-children path below rather
                    # than the "all blocked" one (UX audit F-043).
                    select(ScraperConfig).where(
                        ScraperConfig.batch_id == batch.id,
                        ScraperConfig.user_id == batch.user_id,
                        current_batch_child_clause(),
                    )
                ).scalars().all()
                if not configs:
                    run.status = "done"
                    run.completed_at = datetime.now(UTC)
                    db.commit()
                else:
                    blocked_children = []
                    for c in configs:
                        # A downgrade-paused child never runs. config_run_violation
                        # only counts ACTIVE configs, so it can find nothing wrong
                        # with a paused one, and should_block_run also depends on
                        # the enforcement flag. Neither may decide this: report it
                        # as a plan limit and give it no Job. Keyed on the reason
                        # alone, so a row that contradicts itself (active=True
                        # with the pause reason still set) is blocked, not billed.
                        if c.paused_reason == PAUSED_REASON_ENTITLEMENT:
                            blocked_children.append({
                                "config_id": str(c.id),
                                "county": c.county,
                                "record_type": c.record_type,
                                "reason": "plan limit",
                            })
                            continue
                        _active = db.execute(
                            select(
                                ScraperConfig.id, ScraperConfig.state, ScraperConfig.county,
                                ScraperConfig.record_type, ScraperConfig.created_at,
                                ScraperConfig.active, ScraperConfig.paused_reason,
                            ).where(ScraperConfig.user_id == c.user_id, ScraperConfig.active)
                        ).all()
                        _violation = config_run_violation(
                            user.plan if user else "starter", c.state, c.county,
                            c.record_type, [ConfigRow(*r) for r in _active],
                        )
                        if should_block_run(_violation, user_id=str(c.user_id),
                                            plan=(user.plan if user else "starter"), context="batch_fanout"):
                            blocked_children.append({
                                "config_id": str(c.id),
                                "county": c.county,
                                "record_type": c.record_type,
                                "reason": "plan limit",
                            })
                            continue
                        job = Job(
                            id=str(uuid.uuid4()),
                            user_id=c.user_id,
                            scraper_config_id=c.id,
                            status="pending",
                            trigger="batch",
                        )
                        # A child cancelled mid-run whose worker has not stopped yet:
                        # the run-slot rule every start path shares (F-003). The
                        # index below only covers ACTIVE runs.
                        stopping = db.execute(
                            select(Job.id).where(
                                Job.scraper_config_id == c.id,
                                Job.user_id == c.user_id,
                                Job.status == "cancelled",
                                Job.holds_run_slot(),
                            ).limit(1)
                        ).scalar()
                        if stopping is not None:
                            blocked_children.append({
                                "config_id": str(c.id),
                                "county": c.county,
                                "record_type": c.record_type,
                                "reason": "still stopping",
                                "job_id": str(stopping),
                            })
                            continue
                        # One active run per scraper (migration 104). A child that
                        # is already running (e.g. its own "Run now") must not
                        # abort the whole fan-out: insert each child in a SAVEPOINT
                        # so a clash rolls back that child only, keeping the run
                        # row's lock and every sibling. Flush first so the
                        # savepoint holds nothing but this child.
                        db.flush()
                        try:
                            with db.begin_nested():
                                db.add(job)
                                db.flush()
                        except IntegrityError as exc:
                            if not _is_one_active_job_violation(exc):
                                raise
                            running = db.execute(
                                select(Job.id).where(
                                    Job.scraper_config_id == c.id,
                                    Job.user_id == c.user_id,
                                    Job.status.in_(ACTIVE_STATUSES),
                                ).limit(1)
                            ).scalar()
                            blocked_children.append({
                                "config_id": str(c.id),
                                "county": c.county,
                                "record_type": c.record_type,
                                "reason": "already running",
                                "job_id": str(running) if running else None,
                            })
                            continue
                        enqueued.append(str(job.id))
                    if not enqueued:
                        # Every child config was blocked (plan limits, already
                        # running, or still stopping), so no child jobs exist to
                        # fire the completion barrier. Terminalize
                        # as failed (mirrors the monthly-record-limit branch above)
                        # instead of leaving the run "running" forever.
                        run.status = "failed"
                        run.failed_children = blocked_children or [
                            {"reason": "all batch configs blocked by plan limits"}
                        ]
                        run.completed_at = datetime.now(UTC)
                        db.commit()
                    else:
                        run.child_job_ids = enqueued
                        run.failed_children = blocked_children or None
                        run.status = "running"
                        run.running_at = datetime.now(UTC)  # stuck-time baseline (P1)
                        db.commit()
        elif run.status == "running":
            # RECOVERY: a duplicate/retried dispatch of an already-materialized run.
            # Re-enqueue any child jobs committed but maybe not dispatched (crash
            # between commit and .delay). Idempotent + safe (see _pending_child_ids:
            # run_scrape_job's atomic claim makes a re-enqueue a no-op if in flight).
            # dispatch_attempts is bumped by batch_recovery_sweep, not here.
            enqueued = _pending_child_ids(db, run)
            db.commit()
        else:
            # terminal (done/failed/partial/cancelled) — nothing to dispatch.
            pass

    # Enqueue AFTER commit so a worker can't pick up an uncommitted job row.
    # Every child of a batch belongs to the batch's owner, so one queue decision
    # covers the whole fan-out. This used to be run_scrape_job.delay(jid), which
    # routes to `scrape` for every plan and left a Business/Agency batch, the
    # single largest thing they can run, off the priority queue they pay for.
    for jid in enqueued:
        run_scrape_job.apply_async(args=[jid], queue=queue)
    _logger.info(
        "dispatch_batch_run %s: dispatched %d child jobs to %s",
        run_id, len(enqueued), queue,
    )
