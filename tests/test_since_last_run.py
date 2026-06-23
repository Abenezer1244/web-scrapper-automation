"""DB-backed tests for since_last_run date resolution (Q5 fix).

Proves the window starts the day AFTER the last run's COVERED window end
(its date_to), not its finish timestamp, with a fallback chain. Real Postgres
(mirrors test_scheduled_job_dedup style).
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from src.api.auth import hash_password
from src.db.models import Job, ScraperConfig, User
from src.db.session import SyncSessionLocal
from src.workers.tasks_helpers.dates import _resolve_date_range


def _user(db: Session) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"since_last_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
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
        name="Since Last Run Test",
        county="king",
        state="WA",
        record_type="probate",
        fields=["party_name"],
        enrichment=[],
        schedule={"date_range_mode": "since_last_run"},
        deliver={},
    )
    db.add(config)
    db.flush()
    return config


def _done_job(db: Session, config: ScraperConfig, *, date_to, finished_at) -> Job:
    job = Job(
        id=str(uuid.uuid4()),
        user_id=config.user_id,
        scraper_config_id=config.id,
        status="done",
        trigger="scheduled",
        date_to=date_to,
        finished_at=finished_at,
    )
    db.add(job)
    db.flush()
    return job


def test_since_last_run_starts_day_after_covered_date_to():
    # Covered window ended 01/10/2026 -> next scrape starts 01/11/2026, even
    # though the job FINISHED much later (06/01) — finish time must not be used.
    with SyncSessionLocal() as db:
        config = _config(db, _user(db).id)
        _done_job(
            db, config,
            date_to="01/10/2026",
            finished_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        )
        db.commit()
        df, dt = _resolve_date_range(
            {"date_range_mode": "since_last_run"},
            config_id=config.id, job_id=str(uuid.uuid4()), user_plan="pro",
        )
        assert df == "01/11/2026"
        # Chronological (not lexicographic) ordering check.
        assert datetime.strptime(df, "%m/%d/%Y") <= datetime.strptime(dt, "%m/%d/%Y")


def test_since_last_run_uses_max_covered_window_not_latest_finish():
    # Codex P1: a backfill job that FINISHED later but covered an OLDER window
    # must NOT rewind the resume point. Job A covered through 06/10 (finished
    # 06/10); Job B covered through 01/10 but finished LATER (06/11). Resume must
    # be 06/11 (day after A's 06/10), not 01/11.
    with SyncSessionLocal() as db:
        config = _config(db, _user(db).id)
        _done_job(db, config, date_to="06/10/2026",
                  finished_at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC))
        _done_job(db, config, date_to="01/10/2026",
                  finished_at=datetime(2026, 6, 11, 12, 0, tzinfo=UTC))  # finished later
        db.commit()
        df, _dt = _resolve_date_range(
            {"date_range_mode": "since_last_run"},
            config_id=config.id, job_id=str(uuid.uuid4()), user_plan="pro",
        )
        assert df == "06/11/2026"


def test_since_last_run_falls_back_to_finished_at_when_no_date_to():
    # Legacy job with no stored date_to falls back to finished_at.date()+1.
    with SyncSessionLocal() as db:
        config = _config(db, _user(db).id)
        _done_job(
            db, config,
            date_to=None,
            finished_at=datetime(2026, 2, 15, 9, 0, tzinfo=UTC),
        )
        db.commit()
        df, _dt = _resolve_date_range(
            {"date_range_mode": "since_last_run"},
            config_id=config.id, job_id=str(uuid.uuid4()), user_plan="pro",
        )
        assert df == "02/16/2026"


def test_since_last_run_ignores_shaped_but_invalid_date_to():
    # A shaped-but-calendar-invalid date_to ("11/31/2026", Nov has 30 days) that
    # Postgres to_date would normalize to a LATER date (12/01) must be excluded
    # by the round-trip guard, so the real covered window (06/10) wins.
    with SyncSessionLocal() as db:
        config = _config(db, _user(db).id)
        _done_job(db, config, date_to="06/10/2026",
                  finished_at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC))
        _done_job(db, config, date_to="11/31/2026",
                  finished_at=datetime(2026, 6, 11, 12, 0, tzinfo=UTC))
        db.commit()
        df, _dt = _resolve_date_range(
            {"date_range_mode": "since_last_run"},
            config_id=config.id, job_id=str(uuid.uuid4()), user_plan="pro",
        )
        assert df == "06/11/2026"


def test_since_last_run_no_previous_job_defaults_to_30_days():
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    with SyncSessionLocal() as db:
        config = _config(db, _user(db).id)
        db.commit()
        df, dt = _resolve_date_range(
            {"date_range_mode": "since_last_run"},
            config_id=config.id, job_id=str(uuid.uuid4()), user_plan="pro",
        )
        expected_from = (datetime.now(ZoneInfo("US/Pacific")).date() - timedelta(days=30)).strftime("%m/%d/%Y")
        assert df == expected_from
