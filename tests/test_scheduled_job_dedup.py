"""DB-backed tests for _scheduled_dispatch_blocker_exists — the scheduled-job
dedup guard (Q2 fix).

Proves the ±1-minute beat window can't double-fire a fast-completing scheduled
scrape, while keeping the existing overlap guard and NOT letting a manual run
suppress the scheduled occurrence. Real Postgres (mirrors test_batch_2b_scheduled
style) with a FIXED `now` — no real-clock flakiness.
"""
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from src.api.auth import hash_password
from src.db.models import Job, ScraperConfig, User
from src.db.session import SyncSessionLocal
from src.workers.scheduler_helpers.dispatch import (
    _SCHEDULED_DEDUP_MINUTES,
    _scheduled_dispatch_blocker_exists,
)

NOW = datetime(2026, 6, 12, 6, 0, tzinfo=UTC)


def _user(db: Session) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"sched_dedup_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"),
        plan="pro",
    )
    db.add(user)
    db.flush()
    return user


def _config(db: Session, user_id: str) -> ScraperConfig:
    config = ScraperConfig(
        id=str(uuid.uuid4()),
        user_id=user_id,
        name="Sched Dedup Test",
        county="king",
        state="WA",
        record_type="probate",
        fields=["party_name"],
        enrichment=[],
        schedule={"frequency": "daily", "run_at_hour": 6, "run_at_minute": 0},
        deliver={},
    )
    db.add(config)
    db.flush()
    return config


def _job(db: Session, config: ScraperConfig, *, status: str, trigger: str, created_at: datetime) -> Job:
    job = Job(
        id=str(uuid.uuid4()),
        user_id=config.user_id,
        scraper_config_id=config.id,
        status=status,
        trigger=trigger,
        created_at=created_at,
    )
    db.add(job)
    db.flush()
    return job


def test_no_jobs_allows_dispatch():
    with SyncSessionLocal() as db:
        config = _config(db, _user(db).id)
        assert _scheduled_dispatch_blocker_exists(db, config.id, NOW) is False


def test_active_job_any_trigger_blocks():
    # Overlap guard: a manual run still in flight must block a scheduled fire.
    with SyncSessionLocal() as db:
        config = _config(db, _user(db).id)
        _job(db, config, status="scraping", trigger="manual", created_at=NOW)
        assert _scheduled_dispatch_blocker_exists(db, config.id, NOW) is True


def test_recent_finished_scheduled_job_blocks_duplicate():
    # The fix: a scheduled scrape that already finished (done, not active) inside
    # the window must still block the next tick from creating a duplicate.
    with SyncSessionLocal() as db:
        config = _config(db, _user(db).id)
        _job(db, config, status="done", trigger="scheduled", created_at=NOW - timedelta(minutes=1))
        assert _scheduled_dispatch_blocker_exists(db, config.id, NOW) is True


def test_old_finished_scheduled_job_allows_next_occurrence():
    # A scheduled job from outside the window (e.g. yesterday's run) must NOT
    # block today's occurrence.
    with SyncSessionLocal() as db:
        config = _config(db, _user(db).id)
        old = NOW - timedelta(minutes=_SCHEDULED_DEDUP_MINUTES + 5)
        _job(db, config, status="done", trigger="scheduled", created_at=old)
        assert _scheduled_dispatch_blocker_exists(db, config.id, NOW) is False


def test_recent_finished_manual_job_does_not_suppress_schedule():
    # A finished manual "Run now" must not eat the scheduled occurrence.
    with SyncSessionLocal() as db:
        config = _config(db, _user(db).id)
        _job(db, config, status="done", trigger="manual", created_at=NOW - timedelta(minutes=1))
        assert _scheduled_dispatch_blocker_exists(db, config.id, NOW) is False
