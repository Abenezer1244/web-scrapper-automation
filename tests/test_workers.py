"""Tests for Celery workers: watchdog, monthly reset, and delivery."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from src.db.models import Job, ScraperConfig, User
from src.db.session import SyncSessionLocal
from src.utils.data_exporter import DataExporter, _build_dataframe, _COLUMN_ORDER
from src.api.auth import hash_password


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _create_sync_user(db: Session, plan: str = "starter", records_used: int = 0) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"worker_test_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"),
        plan=plan,
        records_used=records_used,
        records_limit=50,
    )
    db.add(user)
    db.flush()
    return user


def _create_sync_config(db: Session, user_id: str) -> ScraperConfig:
    config = ScraperConfig(
        id=str(uuid.uuid4()),
        user_id=user_id,
        name="Worker Test Config",
        county="pierce",
        state="WA",
        record_type="probate",
        fields=[],
        enrichment=[],
        schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    db.flush()
    return config


def _create_stuck_job(db: Session, user_id: str, config_id: str, minutes_ago: int = 35) -> Job:
    job = Job(
        id=str(uuid.uuid4()),
        user_id=user_id,
        scraper_config_id=config_id,
        status="scraping",
        trigger="scheduled",
        started_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )
    db.add(job)
    db.flush()
    return job


# ─── DataFrame / export ───────────────────────────────────────────────────────

def test_build_dataframe_column_order():
    records = [{"date_recorded": "01/01/2024", "party_name": "Test", "parcel_id": "1111111111"}]
    df = _build_dataframe(records)
    present = [c for c in _COLUMN_ORDER if c in df.columns]
    assert list(df.columns[:len(present)]) == present


def test_build_dataframe_sanitizes_formulas():
    records = [{"party_name": "=SUM(A1)", "parcel_id": "1234567890"}]
    df = _build_dataframe(records)
    assert not df["party_name"].iloc[0].startswith("=")


def test_export_csv_real_file(tmp_path):
    exporter = DataExporter(export_dir=str(tmp_path))
    records = [{"date_recorded": "01/01/2024", "party_name": "Smith", "parcel_id": "1111111111"}]
    path = exporter.export(records, filename="worker_test", fmt="csv")
    assert path.exists()
    assert path.stat().st_size > 0


def test_export_excel_real_file(tmp_path):
    exporter = DataExporter(export_dir=str(tmp_path))
    records = [{"date_recorded": "01/01/2024", "party_name": "Jones", "parcel_id": "2222222222"}]
    path = exporter.export(records, filename="worker_test", fmt="excel")
    assert path.exists()
    assert path.suffix == ".xlsx"


def test_export_json_real_file(tmp_path):
    import json
    exporter = DataExporter(export_dir=str(tmp_path))
    records = [{"date_recorded": "01/01/2024", "party_name": "Doe", "parcel_id": "3333333333"}]
    path = exporter.export(records, filename="worker_test", fmt="json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert len(data) == 1


# ─── Watchdog: stuck job detection ────────────────────────────────────────────

def test_watchdog_requeues_stuck_job():
    """A job stuck for > 30 min with retry_count=0 should be reset to pending."""
    from src.workers.scheduler import watchdog_stuck_jobs

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        job = _create_stuck_job(db, user.id, config.id, minutes_ago=35)
        job_id = job.id
        db.commit()

    watchdog_stuck_jobs()

    with SyncSessionLocal() as db:
        refreshed = db.get(Job, job_id)
        assert refreshed.status == "pending"
        assert refreshed.retry_count == 1
        assert refreshed.started_at is None


def test_watchdog_permanently_fails_after_max_retries():
    """A job at retry_count=3 must be marked failed, not re-queued."""
    from src.workers.scheduler import watchdog_stuck_jobs

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        job = _create_stuck_job(db, user.id, config.id, minutes_ago=35)
        job.retry_count = 3
        job_id = job.id
        db.commit()

    watchdog_stuck_jobs()

    with SyncSessionLocal() as db:
        refreshed = db.get(Job, job_id)
        assert refreshed.status == "failed"
        assert refreshed.error_message is not None
        assert refreshed.finished_at is not None


def test_watchdog_ignores_recent_jobs():
    """A job running for only 5 minutes must not be touched."""
    from src.workers.scheduler import watchdog_stuck_jobs

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        job = _create_stuck_job(db, user.id, config.id, minutes_ago=5)
        job_id = job.id
        db.commit()

    watchdog_stuck_jobs()

    with SyncSessionLocal() as db:
        refreshed = db.get(Job, job_id)
        assert refreshed.status == "scraping"  # unchanged


# ─── Monthly reset ────────────────────────────────────────────────────────────

def test_monthly_reset_clears_records_used():
    """reset_monthly_usage must set records_used = 0 for all users."""
    from src.workers.scheduler import reset_monthly_usage

    with SyncSessionLocal() as db:
        user = _create_sync_user(db, records_used=42)
        user_id = user.id
        db.commit()

    reset_monthly_usage()

    with SyncSessionLocal() as db:
        refreshed = db.get(User, user_id)
        assert refreshed.records_used == 0


def test_monthly_reset_affects_all_users():
    """All users must be reset, not just one."""
    from src.workers.scheduler import reset_monthly_usage

    ids = []
    with SyncSessionLocal() as db:
        for _ in range(3):
            u = _create_sync_user(db, records_used=100)
            ids.append(u.id)
        db.commit()

    reset_monthly_usage()

    with SyncSessionLocal() as db:
        for uid in ids:
            u = db.get(User, uid)
            assert u.records_used == 0


# ─── Delivery: payment failed email ───────────────────────────────────────────

def test_send_payment_failed_email_soft_fails_gracefully():
    """With a fake RESEND_API_KEY, the function must not raise — only log."""
    from src.workers.delivery import _send_payment_failed_email
    # RESEND_API_KEY is set to "re_fake" in CI — call will fail silently
    _send_payment_failed_email("test@test.bridgeleads.io", attempt_count=1)
    # If we reach here without an exception the test passes


def test_deliver_job_results_soft_fails_gracefully():
    """With a fake RESEND_API_KEY, delivery must not raise."""
    from src.workers.delivery import deliver_job_results
    deliver_job_results(
        job_id=str(uuid.uuid4()),
        scraper_name="Pierce County Probate",
        record_count=10,
        download_url="https://example.com/fake",
        recipient_emails=["test@test.bridgeleads.io"],
        fmt="csv",
    )
