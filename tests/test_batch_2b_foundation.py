"""2B foundation (migration 052): plural runs per batch + run-scoped dispatch.

DB-backed (real Postgres, like test_batch_dispatch). Locks the new invariants:
  - terminal history rows accumulate freely; only ONE active (pending/running)
    run per batch (partial unique uq_batch_runs_one_active)
  - occurrence idempotency: one run per (batch_id, scheduled_for); NULL
    (on-demand) is exempt
  - dispatch_batch_run takes a RUN id (new contract) and still resolves a
    legacy BATCH id (transitional path) — including when the batch has
    terminal history (the exact case that raised MultipleResultsFound pre-2B)
  - dispatch's terminal writes set completed_at (consistency with finalize)
"""
import uuid

import pytest
from kombu.exceptions import OperationalError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.api.auth import hash_password
from src.db.models import BatchRun, Job, ScraperBatch, ScraperConfig, User
from src.db.session import SyncSessionLocal
from src.workers.batch_tasks import dispatch_batch_run


def _dispatch(ref: str) -> None:
    """Run dispatch_batch_run, tolerating a post-commit broker enqueue failure
    (same hermetic pattern as test_batch_dispatch)."""
    try:
        dispatch_batch_run(ref)
    except OperationalError:
        pass


def _user(db: Session, records_used: int = 0, records_limit: int = 50) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"batch_2b_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"),
        plan="pro",
        records_used=records_used,
        records_limit=records_limit,
    )
    db.add(user)
    db.flush()
    return user


def _batch(db: Session, user_id: str, n_children: int = 1) -> str:
    batch = ScraperBatch(
        id=str(uuid.uuid4()),
        user_id=user_id,
        name="2B Foundation Test",
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
    db.commit()
    return batch.id


def _run(db: Session, batch_id: str, user_id: str, status: str, **kw) -> BatchRun:
    run = BatchRun(
        id=str(uuid.uuid4()),
        batch_id=batch_id,
        user_id=user_id,
        status=status,
        child_job_ids=[],
        **kw,
    )
    db.add(run)
    return run


def test_terminal_history_plus_one_active_allowed():
    """A batch may hold N terminal runs AND one active run (the 2B shape)."""
    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch(db, user.id)
        _run(db, batch_id, user.id, "done")
        _run(db, batch_id, user.id, "failed")
        _run(db, batch_id, user.id, "cancelled")
        _run(db, batch_id, user.id, "pending")  # one active alongside history
        db.commit()
        n = db.query(BatchRun).filter(BatchRun.batch_id == batch_id).count()
        assert n == 4


def test_second_active_run_rejected():
    """The partial unique allows at most ONE pending/running run per batch."""
    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch(db, user.id)
        _run(db, batch_id, user.id, "running")
        db.commit()
        _run(db, batch_id, user.id, "pending")
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_occurrence_unique_blocks_same_tick_twice():
    """One run per (batch, scheduled_for) — the fast-batch re-fire guard. The
    second insert is also a SECOND ACTIVE run, so make the first terminal to
    prove the OCCURRENCE unique (not the active-run index) is what rejects."""
    from datetime import UTC, datetime

    tick = datetime(2026, 6, 12, 6, 0, tzinfo=UTC)
    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch(db, user.id)
        _run(db, batch_id, user.id, "done", scheduled_for=tick)  # occurrence ran fast
        db.commit()
        _run(db, batch_id, user.id, "pending", scheduled_for=tick)  # same tick again
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_null_scheduled_for_repeats_freely():
    """On-demand runs (scheduled_for NULL) are exempt from the occurrence unique."""
    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch(db, user.id)
        _run(db, batch_id, user.id, "done")       # scheduled_for NULL
        _run(db, batch_id, user.id, "partial")    # scheduled_for NULL again
        db.commit()
        n = db.query(BatchRun).filter(BatchRun.batch_id == batch_id).count()
        assert n == 2


def test_dispatch_by_run_id_materializes():
    """New contract: dispatch by RUN id flips pending->running + creates jobs."""
    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch(db, user.id, n_children=2)
        run = _run(db, batch_id, user.id, "pending")
        db.commit()
        run_id = run.id

    _dispatch(run_id)

    with SyncSessionLocal() as db:
        run = db.get(BatchRun, run_id)
        assert run.status == "running"
        assert len(run.child_job_ids) == 2
        jobs = db.query(Job).filter(Job.id.in_(run.child_job_ids)).all()
        assert all(j.status == "pending" and j.trigger == "batch" for j in jobs)


def test_dispatch_transitional_batch_id_with_history():
    """Legacy payloads carry a BATCH id. With terminal history present this used
    to raise MultipleResultsFound; the transitional resolver must pick the ACTIVE
    run and materialize it."""
    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch(db, user.id, n_children=1)
        _run(db, batch_id, user.id, "done")            # history
        active = _run(db, batch_id, user.id, "pending")
        db.commit()
        active_id = active.id

    _dispatch(batch_id)  # legacy ref

    with SyncSessionLocal() as db:
        active = db.get(BatchRun, active_id)
        assert active.status == "running"
        assert len(active.child_job_ids) == 1
        # History untouched.
        done = (
            db.query(BatchRun)
            .filter(BatchRun.batch_id == batch_id, BatchRun.status == "done")
            .one()
        )
        assert done.child_job_ids == []


def test_dispatch_terminal_write_sets_completed_at():
    """Over-limit dispatch fails the run AND stamps completed_at (terminal-state
    consistency with finalize_batch_run)."""
    with SyncSessionLocal() as db:
        user = _user(db, records_used=50, records_limit=50)
        batch_id = _batch(db, user.id, n_children=1)
        run = _run(db, batch_id, user.id, "pending")
        db.commit()
        run_id = run.id

    _dispatch(run_id)

    with SyncSessionLocal() as db:
        run = db.get(BatchRun, run_id)
        assert run.status == "failed"
        assert run.completed_at is not None
