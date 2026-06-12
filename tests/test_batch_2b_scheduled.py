"""2B Phase 2: dispatch_scheduled_batches — scheduled batches fire durable runs.

DB-backed (real Postgres). Exercises the testable core _dispatch_due_batches
with a FIXED `now` (no real-clock flakiness): due-ness, the occurrence key
(target minute, not tick minute), durable idempotency across adjacent ticks,
the active-run guard, and the record-limit gate.
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from src.api.auth import hash_password
from src.api.schemas import BatchCreateRequest
from src.db.models import BatchRun, ScraperBatch, User
from src.db.session import SyncSessionLocal
from src.workers.scheduler import _dispatch_due_batches

# A fixed tick: 2026-06-12 06:00 UTC (a Friday — daily schedules fire any day).
TICK = datetime(2026, 6, 12, 6, 0, tzinfo=UTC)


def _user(db: Session, records_used: int = 0, records_limit: int = 50) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"batch_2b_sched_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"),
        plan="pro",
        records_used=records_used,
        records_limit=records_limit,
    )
    db.add(user)
    db.flush()
    return user


def _batch(
    db: Session,
    user_id: str,
    frequency: str = "daily",
    hour: int = 6,
    minute: int = 0,
    status: str = "active",
) -> str:
    batch = ScraperBatch(
        id=str(uuid.uuid4()),
        user_id=user_id,
        name="2B Scheduled Test",
        state="WA",
        fields=[],
        enrichment=[],
        schedule={"frequency": frequency, "run_at_hour": hour, "run_at_minute": minute},
        deliver={},
        status=status,
    )
    db.add(batch)
    db.commit()
    return batch.id


def _runs(db: Session, batch_id: str) -> list[BatchRun]:
    return db.query(BatchRun).filter(BatchRun.batch_id == batch_id).all()


def test_due_batch_creates_pending_run_keyed_to_target_minute():
    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch(db, user.id, "daily", 6, 0)

        created = _dispatch_due_batches(db, TICK)
        db.commit()

        runs = _runs(db, batch_id)
        assert len(runs) == 1
        assert runs[0].id in created
        assert runs[0].status == "pending"
        assert runs[0].scheduled_for == TICK  # target minute == this tick


def test_adjacent_tick_in_window_does_not_double_fire():
    """±1-minute window: the 06:01 tick also matches a 06:00 schedule. The
    occurrence key is the TARGET minute, so the second insert no-ops — even
    when the first run already completed (active-run dedupe alone would fail)."""
    tick_0601 = datetime(2026, 6, 12, 6, 1, tzinfo=UTC)
    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch(db, user.id, "daily", 6, 0)

        _dispatch_due_batches(db, TICK)
        db.commit()
        mine = _runs(db, batch_id)
        assert len(mine) == 1
        # The run finishes FAST — terminal before the next tick.
        db.query(BatchRun).filter(BatchRun.id == mine[0].id).update(
            {"status": "done", "completed_at": datetime.now(UTC)}
        )
        db.commit()

        second = _dispatch_due_batches(db, tick_0601)
        db.commit()

        # Scope to THIS batch — the dispatcher sweeps every active batch in the
        # DB, so other tests' batches may legitimately appear in `second`.
        assert len(_runs(db, batch_id)) == 1  # occurrence unique rejected the re-fire


def test_active_run_blocks_new_occurrence():
    """A still-active run from a previous occurrence blocks today's fire (the
    partial unique) — batches never stack concurrent runs."""
    next_day = datetime(2026, 6, 13, 6, 0, tzinfo=UTC)
    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch(db, user.id, "daily", 6, 0)

        _dispatch_due_batches(db, TICK)
        db.commit()
        assert len(_runs(db, batch_id)) == 1  # still 'pending' (never materialized)

        second = _dispatch_due_batches(db, next_day)
        db.commit()

        # Scope to THIS batch (other tests' batches may fire on the new day).
        assert len(_runs(db, batch_id)) == 1


def test_manual_and_archived_batches_never_fire():
    with SyncSessionLocal() as db:
        user = _user(db)
        manual_id = _batch(db, user.id, "manual")
        archived_id = _batch(db, user.id, "daily", 6, 0, status="archived")

        _dispatch_due_batches(db, TICK)
        db.commit()

        assert _runs(db, manual_id) == []
        assert _runs(db, archived_id) == []


def test_not_due_time_does_not_fire():
    with SyncSessionLocal() as db:
        user = _user(db)
        batch_id = _batch(db, user.id, "daily", 12, 30)  # due at 12:30, tick is 06:00

        _dispatch_due_batches(db, TICK)
        db.commit()

        assert _runs(db, batch_id) == []


def test_record_limit_skips_fire():
    with SyncSessionLocal() as db:
        user = _user(db, records_used=50, records_limit=50)
        batch_id = _batch(db, user.id, "daily", 6, 0)

        _dispatch_due_batches(db, TICK)
        db.commit()

        assert _runs(db, batch_id) == []


def test_create_request_schedule_defaults_to_manual():
    """API contract: schedule is optional and defaults to manual (2A behavior);
    a daily schedule round-trips through the validated shape."""
    req = BatchCreateRequest(state="wa", counties=["pierce"], record_types=["probate"])
    assert req.schedule.frequency == "manual"

    req2 = BatchCreateRequest(
        state="wa",
        counties=["pierce"],
        record_types=["probate"],
        schedule={"frequency": "daily", "run_at_hour": 7, "run_at_minute": 15},
    )
    dumped = req2.schedule.model_dump()
    assert dumped["frequency"] == "daily"
    assert dumped["run_at_hour"] == 7
    assert dumped["run_at_minute"] == 15
