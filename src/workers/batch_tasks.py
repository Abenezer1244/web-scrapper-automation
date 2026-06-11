"""Piece 2: batch-scrape fan-out worker (Phase 2A.2).

`dispatch_batch_run` is enqueued by POST /batches AFTER the parent ScraperBatch +
child ScraperConfigs are committed. It creates the BatchRun (system-written, like
dialer_deliveries — never inserted from the app/RLS session) + one Job per child
config, then enqueues run_scrape_job for each — mirroring the scheduler's
job-dispatch (sync session, commit BEFORE .delay so a worker can't pick up an
uncommitted job). The completion barrier (Phase 2A.3) takes over once the
children settle.

Idempotent: if a BatchRun already exists for the batch (a retried task), it does
nothing — at-most-once fan-out.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.db.models import BatchRun, Job, ScraperBatch, ScraperConfig, User
from src.db.session import system_sync_session
from src.utils.logger import setup_logger
from src.workers import app
from src.workers.tasks import run_scrape_job

_logger = setup_logger("worker.batch")


def _pending_child_ids(db, run: "BatchRun") -> list[str]:
    """Child job ids of `run` still in 'pending' — i.e. created but not yet picked
    up. Used to RECOVER the commit-before-delay crash window: re-enqueuing these is
    safe because run_scrape_job advances pending->queued at the very start, so a
    job already in flight is no longer 'pending' (matches acks_late redelivery)."""
    if not run.child_job_ids:
        return []
    rows = db.execute(
        select(Job.id).where(Job.id.in_(run.child_job_ids), Job.status == "pending")
    ).scalars().all()
    return [str(x) for x in rows]


@app.task(name="src.workers.batch_tasks.dispatch_batch_run")
def dispatch_batch_run(batch_id: str) -> None:
    enqueued: list[str] = []
    with system_sync_session() as db:
        batch = db.get(ScraperBatch, batch_id)
        if batch is None:
            _logger.warning("dispatch_batch_run: batch %s not found", batch_id)
            return

        existing = db.execute(
            select(BatchRun).where(BatchRun.batch_id == batch_id)
        ).scalar_one_or_none()

        if existing is None:
            # Quota gate at dispatch — matches the scheduler's enforcement boundary
            # (dispatch_scheduled_jobs skips a user already at/over records_limit).
            # records_used can change between create-time preflight and now (Codex);
            # re-check. -1 = unlimited. Over limit => a terminal run, no jobs.
            user = db.get(User, batch.user_id)
            over_limit = bool(
                user and user.records_limit != -1 and user.records_used >= user.records_limit
            )
            configs = (
                []
                if over_limit
                else db.execute(
                    # Owner-scoped (defense-in-depth on top of the composite FK).
                    select(ScraperConfig).where(
                        ScraperConfig.batch_id == batch_id,
                        ScraperConfig.user_id == batch.user_id,
                    )
                ).scalars().all()
            )
            # Create the run FIRST + flush — UNIQUE(batch_id) makes fan-out
            # at-most-once: a racing duplicate hits the constraint here -> rollback
            # the whole txn (jobs included) -> fall through to RECOVERY (Codex P1).
            run = BatchRun(
                id=str(uuid.uuid4()),
                batch_id=batch_id,
                user_id=batch.user_id,
                status="failed" if over_limit else ("running" if configs else "done"),
                child_job_ids=[],
                failed_children=(
                    [{"reason": "monthly record limit reached"}] if over_limit else None
                ),
            )
            db.add(run)
            try:
                db.flush()
                for c in configs:
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
                run.child_job_ids = enqueued
                db.commit()
            except IntegrityError:
                # Lost the create race — recover from the winner's run below.
                db.rollback()
                enqueued = []
                existing = db.execute(
                    select(BatchRun).where(BatchRun.batch_id == batch_id)
                ).scalar_one_or_none()

        # RECOVERY: a duplicate/retried dispatch (or the lost-race branch). Re-enqueue
        # any child jobs the winner committed but may not have dispatched (crash
        # between commit and .delay). Idempotent + safe (see _pending_child_ids).
        if not enqueued and existing is not None:
            enqueued = _pending_child_ids(db, existing)

    # Enqueue AFTER commit so a worker can't pick up an uncommitted job row.
    for jid in enqueued:
        run_scrape_job.delay(jid)
    _logger.info("dispatch_batch_run %s: dispatched %d child jobs", batch_id, len(enqueued))
