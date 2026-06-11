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
    safe because run_scrape_job claims a job with an ATOMIC compare-and-set
    (UPDATE ... WHERE status='pending'), so a job already in flight is no longer
    'pending' and a re-enqueued duplicate is a no-op (return on rowcount 0)."""
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

        # Lock the run row FOR UPDATE so concurrent dispatches serialize: exactly
        # one transitions pending->running + creates jobs; the rest see 'running'
        # and fall to RECOVERY. The run is normally pre-created 'pending' by the
        # API (durable intent). FOR UPDATE replaces the old INSERT/UNIQUE race now
        # that the row already exists.
        run = db.execute(
            select(BatchRun).where(BatchRun.batch_id == batch_id).with_for_update()
        ).scalar_one_or_none()

        # Back-compat / safety: a batch from an OLD API (pre-intent) or any path
        # that didn't pre-create the run. Create it 'pending' now; UNIQUE(batch_id)
        # keeps it at-most-once under a race, then re-select FOR UPDATE.
        if run is None:
            db.add(
                BatchRun(
                    id=str(uuid.uuid4()),
                    batch_id=batch_id,
                    user_id=batch.user_id,
                    status="pending",
                    child_job_ids=[],
                )
            )
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
            run = db.execute(
                select(BatchRun).where(BatchRun.batch_id == batch_id).with_for_update()
            ).scalar_one_or_none()
            if run is None:
                _logger.warning("dispatch_batch_run: no run for batch %s", batch_id)
                return

        if run.status == "pending":
            # MATERIALIZE the pending intent: create child jobs + flip to running
            # (or a terminal state), all in this one locked transaction.
            run.dispatch_attempts = (run.dispatch_attempts or 0) + 1
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
                db.commit()
            else:
                configs = db.execute(
                    # Owner-scoped (defense-in-depth on top of the composite FK).
                    select(ScraperConfig).where(
                        ScraperConfig.batch_id == batch_id,
                        ScraperConfig.user_id == batch.user_id,
                    )
                ).scalars().all()
                if not configs:
                    run.status = "done"
                    db.commit()
                else:
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
                    run.status = "running"
                    db.commit()
        elif run.status == "running":
            # RECOVERY: a duplicate/retried dispatch of an already-materialized run.
            # Re-enqueue any child jobs committed but maybe not dispatched (crash
            # between commit and .delay). Idempotent + safe (see _pending_child_ids:
            # run_scrape_job's atomic claim makes a re-enqueue a no-op if in flight).
            run.dispatch_attempts = (run.dispatch_attempts or 0) + 1
            enqueued = _pending_child_ids(db, run)
            db.commit()
        else:
            # terminal (done/failed/partial/cancelled) — nothing to dispatch.
            pass

    # Enqueue AFTER commit so a worker can't pick up an uncommitted job row.
    for jid in enqueued:
        run_scrape_job.delay(jid)
    _logger.info("dispatch_batch_run %s: dispatched %d child jobs", batch_id, len(enqueued))
