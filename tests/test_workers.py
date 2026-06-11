"""Tests for Celery workers: watchdog, monthly reset, and delivery."""
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from src.api.auth import hash_password
from src.db.models import Job, ScraperConfig, User
from src.db.session import SyncSessionLocal
from src.utils.data_exporter import DataExporter
from src.utils.lead_export import LEAD_CSV_COLUMNS, build_lead_export_row

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
        started_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )
    db.add(job)
    db.flush()
    return job


# ─── DataFrame / export ───────────────────────────────────────────────────────

def test_canonical_row_has_all_columns():
    row = build_lead_export_row(
        {"date_recorded": "01/01/2024", "party_name": "Test", "parcel_id": "1111111111"}
    )
    assert set(row.keys()) == set(LEAD_CSV_COLUMNS)


def test_canonical_row_sanitizes_formulas():
    row = build_lead_export_row({"party_name": "=SUM(A1)", "parcel_id": "1234567890"})
    assert not row["party_name"].startswith("=")


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


# ─── Atomic job claim (Track A: prevents double-scrape on duplicate delivery) ──

def test_atomic_claim_pending_to_queued_is_at_most_once():
    """run_scrape_job claims a job with an atomic CAS (UPDATE ... WHERE
    status='pending'). A second delivery of the same job_id — Celery redelivery
    or a recovery re-enqueue of a still-'pending' child — must claim nothing
    (rowcount 0), so the scrape never runs twice. This is the exact statement
    run_scrape_job executes for the pending->queued transition."""
    from sqlalchemy import update

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        job = Job(
            id=str(uuid.uuid4()),
            user_id=user.id,
            scraper_config_id=config.id,
            status="pending",
            trigger="batch",
        )
        db.add(job)
        db.commit()
        job_id = job.id

    def _claim() -> int:
        with SyncSessionLocal() as db:
            rc = db.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "pending")
                .values(status="queued", started_at=datetime.now(UTC))
            ).rowcount
            db.commit()
            return rc

    assert _claim() == 1  # first delivery wins
    assert _claim() == 0  # duplicate / recovery re-enqueue claims nothing

    with SyncSessionLocal() as db:
        assert db.get(Job, job_id).status == "queued"


def test_atomic_claim_skips_cancelled_job():
    """A job cancelled before pickup is not 'pending', so the claim CAS rejects
    it (rowcount 0) and the worker won't scrape a cancelled job."""
    from sqlalchemy import update

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        job = Job(
            id=str(uuid.uuid4()),
            user_id=user.id,
            scraper_config_id=config.id,
            status="cancelled",
            trigger="batch",
        )
        db.add(job)
        db.commit()
        job_id = job.id

    with SyncSessionLocal() as db:
        rc = db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "pending")
            .values(status="queued", started_at=datetime.now(UTC))
        ).rowcount
        db.commit()
    assert rc == 0

    with SyncSessionLocal() as db:
        assert db.get(Job, job_id).status == "cancelled"  # untouched


def test_watchdog_repicks_stranded_retry_pending_job():
    """A job a prior watchdog cycle reset to 'pending' whose re-enqueue failed
    (broker hiccup) is stranded: 'pending' is excluded from the normal scan. The
    stranded-retry branch (retry_count>0, started_at NULL, old) re-DELIVERS it
    WITHOUT bumping retry_count, so a broker outage during the watchdog can't
    strand a single scrape and backlog can't burn its retries (Codex P2)."""
    from kombu.exceptions import OperationalError

    from src.workers.scheduler import watchdog_stuck_jobs

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        job = Job(
            id=str(uuid.uuid4()),
            user_id=user.id,
            scraper_config_id=config.id,
            status="pending",
            trigger="scheduled",
            retry_count=1,           # a watchdog-reset retry, not a fresh job
            started_at=None,
            created_at=datetime.now(UTC) - timedelta(minutes=25),  # > stuck_cutoff
        )
        db.add(job)
        db.commit()
        job_id = job.id

    try:
        watchdog_stuck_jobs()
    except OperationalError:
        pass  # post-commit enqueue may hit a rate-limited broker; that's fine

    with SyncSessionLocal() as db:
        refreshed = db.get(Job, job_id)
        # Re-DELIVERED, not counted as a new attempt: retry_count must NOT bump
        # (else backlog + old created_at would burn retries and fail it early).
        assert refreshed.retry_count == 1
        assert refreshed.status == "pending"


def test_watchdog_ignores_fresh_pending_job():
    """A FRESH pending job (retry_count 0) waiting for capacity must NOT be
    re-picked — only watchdog retries (retry_count>0) are eligible."""
    from src.workers.scheduler import watchdog_stuck_jobs

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        job = Job(
            id=str(uuid.uuid4()),
            user_id=user.id,
            scraper_config_id=config.id,
            status="pending",
            trigger="scheduled",
            retry_count=0,
            started_at=None,
            created_at=datetime.now(UTC) - timedelta(minutes=40),
        )
        db.add(job)
        db.commit()
        job_id = job.id

    watchdog_stuck_jobs()  # no re-queue => no broker enqueue

    with SyncSessionLocal() as db:
        refreshed = db.get(Job, job_id)
        assert refreshed.retry_count == 0          # untouched
        assert refreshed.status == "pending"


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
