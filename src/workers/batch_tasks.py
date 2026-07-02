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

from src.db.models import BatchRun, Job, ScraperBatch, ScraperConfig, User
from src.db.session import system_sync_session
from src.utils.logger import setup_logger
from src.workers import app
from src.workers.tasks import run_scrape_job

_logger = setup_logger("worker.batch")

_ACTIVE_RUN_STATUSES = ("pending", "running")


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
    enqueued: list[str] = []
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

        if run.status == "pending":
            # MATERIALIZE the pending intent: create child jobs + flip to running
            # (or a terminal state), all in this one locked transaction.
            # dispatch_attempts is owned by batch_recovery_sweep (the bound on
            # re-dispatch), not bumped here — this is the normal first execution.
            # Quota gate at dispatch — matches the scheduler's enforcement boundary.
            # records_used can change between create-time preflight and now (Codex);
            # re-check. -1 = unlimited. Over limit => a terminal run, no jobs.
            user = db.get(User, batch.user_id)
            over_limit = bool(
                user and user.records_limit != -1 and user.records_used >= user.records_limit
            )
            if over_limit:
                run.status = "failed"
                run.failed_children = [{"reason": "monthly record limit reached"}]
                run.completed_at = datetime.now(UTC)
                db.commit()
            else:
                configs = db.execute(
                    # Owner-scoped (defense-in-depth on top of the composite FK).
                    select(ScraperConfig).where(
                        ScraperConfig.batch_id == batch.id,
                        ScraperConfig.user_id == batch.user_id,
                    )
                ).scalars().all()
                if not configs:
                    run.status = "done"
                    run.completed_at = datetime.now(UTC)
                    db.commit()
                else:
                    from src.api.entitlements import (
                        ConfigRow,
                        config_run_violation,
                        should_block_run,
                    )
                    blocked_children = []
                    for c in configs:
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
                        db.add(job)
                        db.flush()
                        enqueued.append(str(job.id))
                    if not enqueued:
                        # Every child config was blocked by tier-enforcement, so no
                        # child jobs exist to fire the completion barrier. Terminalize
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
    for jid in enqueued:
        run_scrape_job.delay(jid)
    _logger.info("dispatch_batch_run %s: dispatched %d child jobs", run_id, len(enqueued))
