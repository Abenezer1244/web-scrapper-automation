"""Track A: dispatch_batch_run materializes the API-created 'pending' run.

DB-backed (real Postgres, like test_workers). The API now creates the BatchRun
'pending' (durable intent) in the same txn as the batch; dispatch_batch_run
transitions it pending->running and creates the child jobs. These tests assert
that transition + its idempotency (a duplicate dispatch must not create a second
set of jobs).
"""
import uuid

from kombu.exceptions import OperationalError
from sqlalchemy.orm import Session

from src.api.auth import hash_password
from src.db.models import BatchRun, Job, ScraperBatch, ScraperConfig, User
from src.db.session import SyncSessionLocal
from src.workers.batch_tasks import dispatch_batch_run


def _dispatch(batch_id: str) -> None:
    """Run dispatch_batch_run, tolerating a post-commit broker enqueue failure.

    dispatch_batch_run commits the run transition + child jobs BEFORE it calls
    run_scrape_job.delay() in a loop. These tests assert that committed DB state;
    the actual enqueue is a separate side effect whose failure is recoverable by
    batch_recovery_sweep (Track A phase 5). Swallowing a broker OperationalError
    keeps the test hermetic (independent of broker availability) without mocking."""
    try:
        dispatch_batch_run(batch_id)
    except OperationalError:
        pass


def _user(db: Session, records_used: int = 0, records_limit: int = 50) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"batch_dispatch_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"),
        plan="pro",
        records_used=records_used,
        records_limit=records_limit,
    )
    db.add(user)
    db.flush()
    return user


def _batch_with_pending_run(db: Session, user_id: str, n_children: int = 2) -> str:
    """Mirror what POST /batches now persists: a ScraperBatch + N child configs +
    a 'pending' BatchRun, all committed together."""
    batch = ScraperBatch(
        id=str(uuid.uuid4()),
        user_id=user_id,
        name="Dispatch Test",
        state="WA",
        fields=[],
        enrichment=[],
        schedule={},
        deliver={},
        status="active",
    )
    db.add(batch)
    db.flush()
    for i in range(n_children):
        db.add(
            ScraperConfig(
                id=str(uuid.uuid4()),
                user_id=user_id,
                batch_id=batch.id,
                name=f"child {i}",
                county="pierce",
                state="WA",
                record_type="probate",
                fields=[],
                enrichment=[],
                schedule={},
                deliver={},
            )
        )
    db.add(
        BatchRun(
            id=str(uuid.uuid4()),
            batch_id=batch.id,
            user_id=user_id,
            status="pending",
            child_job_ids=[],
        )
    )
    db.commit()
    return batch.id


def test_dispatch_materializes_pending_run():
    """pending -> running, child jobs created, child_job_ids populated."""

    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch_with_pending_run(db, user.id, n_children=2)

    _dispatch(batch_id)

    with SyncSessionLocal() as db:
        run = db.query(BatchRun).filter(BatchRun.batch_id == batch_id).one()
        assert run.status == "running"
        assert len(run.child_job_ids) == 2
        # dispatch_attempts is owned by the recovery sweep, not the normal dispatch.
        assert run.dispatch_attempts == 0
        assert run.running_at is not None  # stuck-time baseline set on materialize
        jobs = db.query(Job).filter(Job.id.in_(run.child_job_ids)).all()
        assert len(jobs) == 2
        assert all(j.status == "pending" and j.trigger == "batch" for j in jobs)


def test_dispatch_is_idempotent_no_duplicate_jobs():
    """A duplicate dispatch of an already-materialized run must NOT create a
    second set of jobs — the run stays 'running' with the same children."""

    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch_with_pending_run(db, user.id, n_children=3)

    _dispatch(batch_id)
    with SyncSessionLocal() as db:
        first_ids = set(
            db.query(BatchRun).filter(BatchRun.batch_id == batch_id).one().child_job_ids
        )

    _dispatch(batch_id)  # duplicate / recovery re-dispatch
    with SyncSessionLocal() as db:
        run = db.query(BatchRun).filter(BatchRun.batch_id == batch_id).one()
        assert run.status == "running"
        assert set(run.child_job_ids) == first_ids  # same jobs, not a new set
        total_batch_jobs = db.query(Job).filter(Job.user_id == user.id).count()
        assert total_batch_jobs == 3  # exactly the original children


def test_dispatch_over_limit_run_fails_with_no_jobs():
    """If the user is over their monthly record limit at dispatch, the pending
    run becomes 'failed' and no child jobs are created."""

    with SyncSessionLocal() as db:
        user = _user(db, records_used=50, records_limit=50)  # at the cap
        batch_id = _batch_with_pending_run(db, user.id, n_children=2)

    _dispatch(batch_id)

    with SyncSessionLocal() as db:
        run = db.query(BatchRun).filter(BatchRun.batch_id == batch_id).one()
        assert run.status == "failed"
        assert run.child_job_ids == []
        assert db.query(Job).filter(Job.user_id == user.id).count() == 0
