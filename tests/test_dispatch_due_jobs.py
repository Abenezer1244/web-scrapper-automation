"""E2E DB-backed tests for _dispatch_due_jobs — the scheduled single-config
dispatch core (Q7).

Exercises the full glue with a FIXED `now` (no real-clock flakiness): due-ness by
frequency + day, the record-limit gate, the dedup blocker, and that a created job
is a durable 'pending' row keyed to the config. Mirrors test_batch_2b_scheduled.
"""
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from src.api.auth import hash_password
from src.db.models import Job, ScraperConfig, User
from src.db.session import SyncSessionLocal
from src.workers.scheduler_helpers.dispatch import _dispatch_due_jobs

# Fixed tick: 2026-06-12 06:00 UTC — a Friday (weekday 4).
TICK = datetime(2026, 6, 12, 6, 0, tzinfo=UTC)


def _user(db: Session, records_used: int = 0, records_limit: int = 50) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"due_jobs_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"),
        plan="pro",
        records_used=records_used,
        records_limit=records_limit,
    )
    db.add(user)
    db.flush()
    return user


def _config(db: Session, user_id: str, schedule: dict, active: bool = True) -> ScraperConfig:
    config = ScraperConfig(
        id=str(uuid.uuid4()),
        user_id=user_id,
        name="Due Jobs Test",
        county="king",
        state="WA",
        record_type="probate",
        fields=["party_name"],
        enrichment=[],
        schedule=schedule,
        deliver={},
        active=active,
    )
    db.add(config)
    db.flush()
    return config


def _jobs(db: Session, config_id: str) -> list[Job]:
    return db.query(Job).filter(Job.scraper_config_id == config_id).all()


def test_due_daily_config_creates_pending_scheduled_job():
    with SyncSessionLocal() as db:
        config = _config(db, _user(db).id, {"frequency": "daily", "run_at_hour": 6, "run_at_minute": 0})
        created = _dispatch_due_jobs(db, TICK)
        db.commit()
        jobs = _jobs(db, config.id)
        assert len(jobs) == 1
        assert jobs[0].id in created
        assert jobs[0].status == "pending"
        assert jobs[0].trigger == "scheduled"


def test_manual_config_never_dispatched():
    with SyncSessionLocal() as db:
        config = _config(db, _user(db).id, {"frequency": "manual", "run_at_hour": 6, "run_at_minute": 0})
        created = _dispatch_due_jobs(db, TICK)
        db.commit()
        assert created == []
        assert _jobs(db, config.id) == []


def test_not_due_time_does_not_fire():
    with SyncSessionLocal() as db:
        config = _config(db, _user(db).id, {"frequency": "daily", "run_at_hour": 7, "run_at_minute": 0})
        created = _dispatch_due_jobs(db, TICK)  # tick is 06:00, target 07:00
        db.commit()
        assert created == []
        assert _jobs(db, config.id) == []


def test_weekly_fires_only_on_chosen_weekday():
    with SyncSessionLocal() as db:
        user_id = _user(db).id
        # TICK is a Friday (weekday 4).
        friday = _config(db, user_id, {"frequency": "weekly", "run_at_hour": 6, "run_at_minute": 0, "run_at_weekday": 4})
        monday = _config(db, user_id, {"frequency": "weekly", "run_at_hour": 6, "run_at_minute": 0, "run_at_weekday": 0})
        _dispatch_due_jobs(db, TICK)
        db.commit()
        assert len(_jobs(db, friday.id)) == 1
        assert len(_jobs(db, monday.id)) == 0


def test_record_limit_skips_dispatch():
    with SyncSessionLocal() as db:
        user = _user(db, records_used=50, records_limit=50)  # at limit
        config = _config(db, user.id, {"frequency": "daily", "run_at_hour": 6, "run_at_minute": 0})
        created = _dispatch_due_jobs(db, TICK)
        db.commit()
        assert created == []
        assert _jobs(db, config.id) == []


def test_dedup_blocks_second_dispatch_in_window():
    with SyncSessionLocal() as db:
        config = _config(db, _user(db).id, {"frequency": "daily", "run_at_hour": 6, "run_at_minute": 0})
        first = _dispatch_due_jobs(db, TICK)
        db.commit()
        assert len(first) == 1
        # An adjacent tick (06:01) within the dedup window must NOT create a 2nd
        # job — even though the first may already be terminal in a fast scrape.
        second = _dispatch_due_jobs(db, TICK + timedelta(minutes=1))
        db.commit()
        assert second == []
        assert len(_jobs(db, config.id)) == 1


def test_dedup_blocks_after_fast_scrape_finishes():
    # The real Q2 fix: even if the first scheduled job already FINISHED (done, no
    # longer active) inside the dedup window, the adjacent tick must not create a
    # second job. This exercises the recent-scheduled branch of the dedup guard,
    # distinct from the active-status branch covered above.
    with SyncSessionLocal() as db:
        config = _config(db, _user(db).id, {"frequency": "daily", "run_at_hour": 6, "run_at_minute": 0})
        first = _dispatch_due_jobs(db, TICK)
        assert len(first) == 1
        # Simulate a fast scrape that completed before the next tick.
        db.query(Job).filter(Job.id == first[0]).update({"status": "done"})
        db.commit()
        second = _dispatch_due_jobs(db, TICK + timedelta(minutes=1))
        db.commit()
        assert second == []
        assert len(_jobs(db, config.id)) == 1


def test_inactive_config_not_dispatched():
    with SyncSessionLocal() as db:
        config = _config(db, _user(db).id, {"frequency": "daily", "run_at_hour": 6, "run_at_minute": 0}, active=False)
        created = _dispatch_due_jobs(db, TICK)
        db.commit()
        assert created == []
        assert _jobs(db, config.id) == []
