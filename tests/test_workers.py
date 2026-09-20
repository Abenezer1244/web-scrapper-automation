"""Tests for Celery workers: watchdog, monthly reset, and delivery."""
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from src.api.auth import hash_password
from src.db.models import Job, ScraperConfig, User
from src.db.session import SyncSessionLocal
from src.utils.data_exporter import DataExporter
from src.utils.lead_export import CRM_V1_LABELS, LEAD_CSV_COLUMNS, build_lead_export_row

# ─── Helpers ─────────────────────────────────────────────────────────────────

def _current_month_start() -> datetime:
    """First instant of the current UTC month — the billing-period boundary."""
    now = datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


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
    # Every legacy AND crm_v1 column, nothing else (one builder serves both layouts).
    assert set(row.keys()) == set(LEAD_CSV_COLUMNS) | set(CRM_V1_LABELS)


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
    """A job stuck past the Celery hard time_limit (70-min cutoff) with
    retry_count=0 should be reset to pending."""
    from src.workers.scheduler import watchdog_stuck_jobs

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        job = _create_stuck_job(db, user.id, config.id, minutes_ago=75)
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
        job = _create_stuck_job(db, user.id, config.id, minutes_ago=75)
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


def test_watchdog_leaves_long_running_live_job_alone():
    """Regression for the 2026-06-17 duplication incident: a job that has been
    actively running for 60 min — LONGER than the old 20-min cutoff but still
    within run_scrape_job's 65-min Celery hard time_limit — is a LIVE job, not a
    dead one. The watchdog must NOT re-queue it (re-queuing a live job made the
    non-idempotent re-run append a second full copy of its results)."""
    from src.workers.scheduler import watchdog_stuck_jobs

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        job = _create_stuck_job(db, user.id, config.id, minutes_ago=60)
        job_id = job.id
        db.commit()

    watchdog_stuck_jobs()

    with SyncSessionLocal() as db:
        refreshed = db.get(Job, job_id)
        assert refreshed.status == "scraping"  # unchanged — still live, not stuck
        assert refreshed.retry_count == 0


# ─── Watchdog: heartbeat-based liveness (Phase 1, migration 061) ──────────────

def test_watchdog_leaves_live_long_job_with_fresh_heartbeat_alone():
    """The core Phase 1 guarantee: a job that has run far longer than the 70-min
    started_at fallback is still LIVE — not stuck — as long as its heartbeat is
    fresh. A 24,708-parcel King enrich legitimately runs > 65 min; with a recent
    heartbeat the watchdog must leave it alone (no false re-queue → no dup)."""
    from src.workers.scheduler import watchdog_stuck_jobs

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        job = _create_stuck_job(db, user.id, config.id, minutes_ago=90)
        job.last_heartbeat_at = datetime.now(UTC) - timedelta(minutes=2)  # alive
        job_id = job.id
        db.commit()

    watchdog_stuck_jobs()

    with SyncSessionLocal() as db:
        refreshed = db.get(Job, job_id)
        assert refreshed.status == "scraping"  # left alone — heartbeat is fresh
        assert refreshed.retry_count == 0


def test_watchdog_requeues_job_with_stale_heartbeat():
    """A job whose heartbeat has gone stale (> 15 min) is genuinely dead (worker
    hard-killed / crashed) and must be re-queued even though its started_at is
    well within the 70-min fallback window."""
    from src.workers.scheduler import watchdog_stuck_jobs

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        job = _create_stuck_job(db, user.id, config.id, minutes_ago=30)
        job.last_heartbeat_at = datetime.now(UTC) - timedelta(minutes=20)  # stale
        job_id = job.id
        db.commit()

    watchdog_stuck_jobs()

    with SyncSessionLocal() as db:
        refreshed = db.get(Job, job_id)
        assert refreshed.status == "pending"  # re-queued
        assert refreshed.retry_count == 1


# ─── Watchdog: recovery writes are guarded against a moved row ────────────────
#
# The watchdog decides in Python from a SELECT and then writes. Everything here
# covers the window between the two: the row can be cancelled, finished,
# re-claimed by a newer attempt, or already recovered by a concurrent tick. Each
# test moves the row from a SECOND session, exactly as another process would,
# while the first session still holds the ORM object the decision was made on
# (SyncSessionLocal sets expire_on_commit=False, so it keeps the observed values).


def _observed_stuck_job(db: Session) -> Job:
    """A stuck job, committed, and returned as the ORM object a SELECT would yield."""
    user = _create_sync_user(db)
    config = _create_sync_config(db, user.id)
    job = _create_stuck_job(db, user.id, config.id, minutes_ago=75)
    db.commit()
    return job


def test_recovery_cas_refuses_a_job_cancelled_since_it_was_selected():
    """THE bug this guard exists for. A cancel that lands between the watchdog's
    SELECT and its write used to be overwritten: the row went cancelled -> pending,
    the watchdog enqueued it, and the worker's pending-only claim CAS — whose whole
    job is to stop a cancelled row being picked up — saw a legitimately pending job
    and ran it. The cancelled run re-scraped, re-billed and re-delivered."""
    from src.workers.scheduler_helpers.health import _recovery_cas

    with SyncSessionLocal() as observer:
        job = _observed_stuck_job(observer)

        # The user cancels, from their own request's session.
        with SyncSessionLocal() as canceller:
            canceller.execute(
                sa_text("UPDATE jobs SET status='cancelled', finished_at=now() WHERE id=:j"),
                {"j": job.id},
            )
            canceller.commit()

        fired = _recovery_cas(
            observer, job,
            retry_count=job.retry_count + 1,
            status="pending",
            started_at=None,
            last_heartbeat_at=None,
            page_current=0,
            page_total=0,
            record_count=0,
        )

    assert fired is False, "a cancelled job must not be re-queued"
    with SyncSessionLocal() as db:
        refreshed = db.get(Job, job.id)
        assert refreshed.status == "cancelled"
        assert refreshed.retry_count == 0  # no retry burned either


def test_recovery_cas_refuses_a_job_that_finished_since_it_was_selected():
    """The mirror case on the permanent-fail branch: stamping 'failed' over a 'done'
    the worker had just committed would tell a customer their run failed while they
    hold the export they were charged for."""
    from src.workers.scheduler_helpers.health import _recovery_cas

    with SyncSessionLocal() as observer:
        job = _observed_stuck_job(observer)
        observer.execute(
            sa_text("UPDATE jobs SET retry_count=3 WHERE id=:j"), {"j": job.id}
        )
        observer.commit()
        job.retry_count = 3  # what the watchdog's SELECT would have read

        with SyncSessionLocal() as worker:
            worker.execute(
                sa_text(
                    "UPDATE jobs SET status='done', finished_at=now(), record_count=57 "
                    "WHERE id=:j"
                ),
                {"j": job.id},
            )
            worker.commit()

        fired = _recovery_cas(
            observer, job,
            status="failed",
            finished_at=datetime.now(UTC),
            error_message="This scraper run did not complete in time.",
        )

    assert fired is False, "a completed job must not be marked failed"
    with SyncSessionLocal() as db:
        refreshed = db.get(Job, job.id)
        assert refreshed.status == "done"
        assert refreshed.record_count == 57
        assert refreshed.error_message is None


def test_recovery_cas_refuses_a_job_reclaimed_by_a_newer_attempt():
    """started_at is the attempt token. A decision taken against a dead attempt must
    not land on the live attempt that has since re-claimed the row — that would null
    the new attempt's started_at and counters out from under a running worker."""
    from src.workers.scheduler_helpers.health import _recovery_cas

    with SyncSessionLocal() as observer:
        job = _observed_stuck_job(observer)

        with SyncSessionLocal() as newer:
            newer.execute(
                sa_text(
                    "UPDATE jobs SET started_at=now(), last_heartbeat_at=now(), "
                    "page_current=3, page_total=5 WHERE id=:j"
                ),
                {"j": job.id},
            )
            newer.commit()

        fired = _recovery_cas(
            observer, job,
            retry_count=job.retry_count + 1,
            status="pending",
            started_at=None,
            last_heartbeat_at=None,
            page_current=0,
            page_total=0,
            record_count=0,
        )

    assert fired is False
    with SyncSessionLocal() as db:
        refreshed = db.get(Job, job.id)
        assert refreshed.status == "scraping"
        assert refreshed.started_at is not None
        assert (refreshed.page_current, refreshed.page_total) == (3, 5)


def test_recovery_cas_refuses_a_second_concurrent_tick():
    """Two watchdog ticks overlapping must not both burn a retry, which would spend
    the budget at twice the rate and fail a recoverable job early."""
    from src.workers.scheduler_helpers.health import _recovery_cas

    with SyncSessionLocal() as observer:
        job = _observed_stuck_job(observer)

        with SyncSessionLocal() as other_tick:
            other_tick.execute(
                sa_text(
                    "UPDATE jobs SET status='pending', retry_count=1, started_at=NULL "
                    "WHERE id=:j"
                ),
                {"j": job.id},
            )
            other_tick.commit()

        fired = _recovery_cas(
            observer, job,
            retry_count=job.retry_count + 1,
            status="pending",
            started_at=None,
            last_heartbeat_at=None,
            page_current=0,
            page_total=0,
            record_count=0,
        )

    assert fired is False
    with SyncSessionLocal() as db:
        assert db.get(Job, job.id).retry_count == 1  # one tick's worth, not two


def test_recovery_cas_applies_when_the_row_has_not_moved():
    """Positive control: the guard must not be so tight that ordinary recovery stops
    working. Without this, every test above would pass on a helper that never writes."""
    from src.workers.scheduler_helpers.health import _recovery_cas

    with SyncSessionLocal() as observer:
        job = _observed_stuck_job(observer)
        observer.execute(
            sa_text(
                "UPDATE jobs SET page_current=3, page_total=5, record_count=42, "
                "last_heartbeat_at=now() WHERE id=:j"
            ),
            {"j": job.id},
        )
        observer.commit()

        fired = _recovery_cas(
            observer, job,
            retry_count=job.retry_count + 1,
            status="pending",
            started_at=None,
            last_heartbeat_at=None,
            page_current=0,
            page_total=0,
            record_count=0,
        )

    assert fired is True
    with SyncSessionLocal() as db:
        refreshed = db.get(Job, job.id)
        assert refreshed.status == "pending"
        assert refreshed.retry_count == 1
        assert refreshed.started_at is None
        assert refreshed.last_heartbeat_at is None
        # Stale progress is cleared so the UI can't read "Page 3 of 5" on a run
        # that is about to start over from the beginning.
        assert (refreshed.page_current, refreshed.page_total, refreshed.record_count) == (0, 0, 0)


def test_watchdog_never_selects_a_cancelled_job():
    """Belt to the CAS's suspenders: 'cancelled' is not in STUCK_CHECK_STATUSES, so a
    cancelled job is not a recovery candidate in the first place, however old it is."""
    from src.workers.scheduler import watchdog_stuck_jobs

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        job = _create_stuck_job(db, user.id, config.id, minutes_ago=600)
        job.status = "cancelled"
        job.finished_at = datetime.now(UTC)
        job_id = job.id
        db.commit()

    watchdog_stuck_jobs()

    with SyncSessionLocal() as db:
        refreshed = db.get(Job, job_id)
        assert refreshed.status == "cancelled"
        assert refreshed.retry_count == 0


def test_heartbeat_write_is_attempt_scoped():
    """_write_heartbeat refreshes only the attempt that matches started_at. A
    thread left over from a superseded attempt (different started_at) updates 0
    rows and self-reaps, so it can't mask a dead new attempt."""
    from src.workers.tasks_helpers.status import (
        _HB_ALIVE,
        _HB_TERMINAL,
        _write_heartbeat,
    )

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        started = datetime.now(UTC) - timedelta(minutes=5)
        job = _create_stuck_job(db, user.id, config.id, minutes_ago=5)
        job.started_at = started
        job.status = "enriching"
        job_id = job.id
        db.commit()

    # Matching started_at → row updated, last_heartbeat_at set.
    assert _write_heartbeat(job_id, started) == _HB_ALIVE
    with SyncSessionLocal() as db:
        assert db.get(Job, job_id).last_heartbeat_at is not None

    # A superseded attempt's started_at → 0 rows → terminal signal (self-reap).
    stale_started = started - timedelta(minutes=30)
    assert _write_heartbeat(job_id, stale_started) == _HB_TERMINAL


# ─── Idempotent result inserts (Phase 2, migration 062) ──────────────────────

def test_result_insert_is_idempotent_on_rerun():
    """A re-run that re-inserts the same (job_id, source_fingerprint) rows via
    ON CONFLICT DO NOTHING appends NO duplicate rows — the 2026-06-17 dup fix."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from src.db.models import Result

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        job = _create_stuck_job(db, user.id, config.id, minutes_ago=1)
        job_id, user_id = job.id, user.id
        db.commit()

        def _row(fp: str) -> dict:
            return {
                "id": str(uuid.uuid4()),
                "job_id": job_id,
                "user_id": user_id,
                "source_fingerprint": fp,
                "is_duplicate": False,
            }

        # Must match production's partial-index arbiter exactly (index_where), or
        # Postgres won't infer uq_results_job_fingerprint as the conflict target.
        # RETURNING lets us count actually-inserted rows: ON CONFLICT DO NOTHING
        # returns rows only for inserts, not for skipped conflicts. (`.rowcount`
        # is unavailable on the ORM insertmanyvalues result — an IteratorResult.)
        stmt = pg_insert(Result).on_conflict_do_nothing(
            index_elements=["job_id", "source_fingerprint"],
            index_where=sa_text("source_fingerprint IS NOT NULL"),
        ).returning(Result.id)
        rows = [_row("fp-a"), _row("fp-b")]
        first = len(db.execute(stmt, rows).scalars().all())
        db.commit()
        # Re-run: same fingerprints, fresh row ids → all conflict → 0 inserted.
        rerun = len(db.execute(stmt, [_row("fp-a"), _row("fp-b")]).scalars().all())
        db.commit()

        total = db.execute(
            sa_text("SELECT count(*) FROM results WHERE job_id = :j"), {"j": job_id}
        ).scalar()

    assert first == 2
    assert rerun == 0
    assert total == 2  # no duplicate copy appended


# ─── Idempotent billing (Phase 3, migration 063) ─────────────────────────────

def test_billing_cas_applies_once_across_reruns():
    """The billing CAS (UPDATE jobs SET billing_applied_at WHERE billing_applied_at
    IS NULL) lets only the first attempt bill; a re-run gets rowcount 0 and must
    not double-charge records_used."""
    from sqlalchemy import update as sa_update

    with SyncSessionLocal() as db:
        user = _create_sync_user(db, records_used=0)
        config = _create_sync_config(db, user.id)
        job = _create_stuck_job(db, user.id, config.id, minutes_ago=1)
        job_id, user_id = job.id, user.id
        db.commit()

        def _bill(amount: int) -> int:
            billed = db.execute(
                sa_update(Job)
                .where(Job.id == job_id, Job.billing_applied_at.is_(None))
                .values(billed_count=amount, billing_applied_at=datetime.now(UTC))
            ).rowcount
            if billed:
                db.execute(
                    sa_update(User)
                    .where(User.id == user_id)
                    .values(records_used=User.records_used + amount)
                )
            db.commit()
            return billed

        first = _bill(10)
        second = _bill(10)  # re-run

        refreshed = db.get(User, user_id)
        job_after = db.get(Job, job_id)

    assert first == 1
    assert second == 0  # CAS blocks the second billing
    assert refreshed.records_used == 10  # charged exactly once
    assert job_after.billed_count == 10


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


def test_set_status_terminal_write_guard_blocks_resurrection():
    """Batch force-finalize cancels a still-running child by flipping its jobs
    row to 'cancelled' from another session. The worker still executing that
    child must NOT later overwrite the cancellation with 'done' (and then bill
    and email for it). _set_status is a CAS over non-terminal rows only: it
    returns False, leaves the row 'cancelled', and refreshes the ORM object so
    the caller sees the real status."""
    from sqlalchemy import update

    from src.workers.tasks import _set_status

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        job = Job(
            id=str(uuid.uuid4()),
            user_id=user.id,
            scraper_config_id=config.id,
            status="enriching",
            trigger="batch",
        )
        db.add(job)
        db.commit()
        job_id = job.id

    with SyncSessionLocal() as worker_db:
        job = worker_db.get(Job, job_id)
        assert job.status == "enriching"  # worker's (soon stale) view

        # Force-finalize terminalizes the row from another session mid-run.
        with SyncSessionLocal() as other:
            other.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(status="cancelled", finished_at=datetime.now(UTC))
            )
            other.commit()

        ok = _set_status(
            worker_db, job, "done",
            finished_at=datetime.now(UTC), record_count=5,
        )
        assert ok is False
        assert job.status == "cancelled"  # refreshed to the DB truth

    with SyncSessionLocal() as db:
        row = db.get(Job, job_id)
        assert row.status == "cancelled"  # never resurrected
        assert row.record_count != 5


def test_set_status_normal_transition_returns_true():
    """The guard must not break the normal lifecycle: a non-terminal row
    transitions and reports success (including terminal transitions FROM a
    non-terminal status, e.g. scraping -> done)."""
    from src.workers.tasks import _set_status

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        job = Job(
            id=str(uuid.uuid4()),
            user_id=user.id,
            scraper_config_id=config.id,
            status="scraping",
            trigger="batch",
        )
        db.add(job)
        db.commit()

        assert _set_status(db, job, "enriching", record_count=3) is True
        assert job.status == "enriching"
        assert job.record_count == 3

        assert _set_status(db, job, "done", finished_at=datetime.now(UTC)) is True
        assert job.status == "done"


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


# --- Entitlement-window reconciliation + skip-trace reset ---------------------
#
# These used to exercise reset_monthly_usage, one task that reset BOTH the record
# counter (on the calendar month) and skip-trace. The records half was retired
# when entitlement windows landed: quota now rolls on each user's own
# anniversary, advanced lazily by the statement that charges and hourly by
# reconcile_quota_periods. Keeping the calendar reset alongside anchored windows
# would zero a 20th-anchored subscriber twice — once on their own boundary and
# again on the 1st.
#
# Every invariant the old tests protected is still asserted below: a stale period
# rolls, a live one is untouched however often the task runs, all stale users
# roll, a NULL period is adopted rather than zeroed, and skip-trace keys on its
# OWN period. They are simply asserted against whichever mechanism now owns each.

def _window(user, start, end) -> None:
    """Put a user in an explicit entitlement window (mirror column kept in step)."""
    user.quota_anchor_at = start
    user.quota_period_start = start
    user.quota_period_end = end
    user.records_period_start = start


def test_reconciliation_rolls_over_an_ended_window():
    """A window whose end has passed is advanced: counter zeroed, window moved."""
    from src.workers.scheduler import reconcile_quota_periods

    with SyncSessionLocal() as db:
        user = _create_sync_user(db, records_used=42)
        _window(user, datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 2, 1, tzinfo=UTC))
        user_id = user.id
        db.commit()

    reconcile_quota_periods()

    with SyncSessionLocal() as db:
        refreshed = db.get(User, user_id)
        assert refreshed.records_used == 0
        # Lands on the window containing NOW — not the next cell after the one it
        # left, and not five years of accumulated buckets.
        assert refreshed.quota_period_start <= datetime.now(UTC)
        assert refreshed.quota_period_end > datetime.now(UTC)
        # records_period_start is a mirror of the window start for one release.
        assert refreshed.records_period_start == refreshed.quota_period_start


def test_reconciliation_does_NOT_touch_a_live_window():
    """The regression guard, unchanged in spirit.

    The old rollover zeroed every user it could match, which destroyed usage
    belonging to the CURRENT period two ways: a new user whose period_start was
    NULL, and a late catch-up run after Beat missed the 1st. Both wiped real,
    already-billed consumption. A user whose window is still open must come
    through untouched no matter how many times the task runs.
    """
    from src.workers.scheduler import reconcile_quota_periods

    now = datetime.now(UTC)
    with SyncSessionLocal() as db:
        user = _create_sync_user(db, records_used=999)
        _window(user, now - timedelta(days=3), now + timedelta(days=20))
        user_id = user.id
        db.commit()

    reconcile_quota_periods()
    reconcile_quota_periods()  # idempotent: running twice must not zero either

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 999


def test_a_late_reconciliation_cannot_wipe_new_window_usage():
    """The second production defect, pinned against the new mechanism.

    Running late is exactly when usage already exists in the NEW window: the
    charging statement advances the window itself, so by the time the beat
    catches up the counter holds consumption belonging to the live window. A
    reconciliation that zeroed on a stale-looking column would destroy it.
    """
    from src.workers.scheduler import reconcile_quota_periods

    now = datetime.now(UTC)
    with SyncSessionLocal() as db:
        user = _create_sync_user(db, records_used=67)
        # The window a job already rolled this user into, moments ago.
        _window(user, now - timedelta(minutes=5), now + timedelta(days=29))
        user_id = user.id
        db.commit()

    reconcile_quota_periods()  # the late catch-up

    with SyncSessionLocal() as db:
        assert db.get(User, user_id).records_used == 67


def test_reconciliation_grants_ONE_window_after_months_away():
    """Unused entitlement must never accumulate.

    A user whose last window ended long ago comes back to the CURRENT window's
    allowance, not one bucket per month they were absent. The counter is zeroed
    exactly once, wherever the window lands.
    """
    from src.workers.scheduler import reconcile_quota_periods

    now = datetime.now(UTC)
    anchor = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=200)
    with SyncSessionLocal() as db:
        user = _create_sync_user(db, records_used=48)
        _window(user, anchor, anchor + timedelta(days=30))
        user_id = user.id
        db.commit()

    reconcile_quota_periods()

    with SyncSessionLocal() as db:
        refreshed = db.get(User, user_id)
        assert refreshed.records_used == 0
        assert refreshed.quota_period_start <= now < refreshed.quota_period_end
        # One month wide, not the whole absence.
        assert (refreshed.quota_period_end - refreshed.quota_period_start) < timedelta(days=32)


def test_reconciliation_does_not_roll_a_frozen_account():
    """A subscription that stopped paying must not accrue a bucket a month.

    Its window stays put until payment recovers, at which point it advances to
    the window containing NOW — one bucket, not one per frozen month.
    """
    from src.workers.scheduler import reconcile_quota_periods

    with SyncSessionLocal() as db:
        user = _create_sync_user(db, records_used=50)
        _window(user, datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 2, 1, tzinfo=UTC))
        user.subscription_status = "unpaid"
        user_id = user.id
        db.commit()

    reconcile_quota_periods()

    with SyncSessionLocal() as db:
        refreshed = db.get(User, user_id)
        assert refreshed.records_used == 50, "a frozen account gets no fresh quota"
        assert refreshed.quota_period_end == datetime(2020, 2, 1, tzinfo=UTC)


def test_reconciliation_retires_a_lapsed_entitlement_without_a_webhook():
    """A customer.subscription.deleted that never arrived must not strand anyone.

    entitlement_ends_at is what holds the window while a cancellation is pending.
    If the webhook is lost nothing would ever clear it and the account would stay
    frozen forever — so the reconciliation performs the downgrade itself, which
    also releases the window.
    """
    from src.workers.scheduler import reconcile_quota_periods

    with SyncSessionLocal() as db:
        user = _create_sync_user(db, plan="pro", records_used=10)
        user.records_limit = 1000
        _window(user, datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 2, 1, tzinfo=UTC))
        user.entitlement_ends_at = datetime(2020, 2, 1, tzinfo=UTC)
        user.subscription_status = "active"
        user.stripe_subscription_id = "sub_gone"
        user_id = user.id
        db.commit()

    reconcile_quota_periods()

    with SyncSessionLocal() as db:
        refreshed = db.get(User, user_id)
        assert refreshed.plan == "starter"
        assert refreshed.entitlement_ends_at is None, "must release the window"
        assert refreshed.paid_entitlement_ended_at is not None
        assert refreshed.stripe_subscription_id is None
        # Released in the same pass, so the user is not left a month behind.
        assert refreshed.quota_period_end > datetime.now(UTC)


def test_reconciliation_applies_a_pending_downgrade_at_the_boundary():
    """P5: a downgrade is deferred, then applied by the rollover — never before."""
    from src.workers.scheduler import reconcile_quota_periods

    with SyncSessionLocal() as db:
        user = _create_sync_user(db, plan="business", records_used=3000)
        user.records_limit = 5000
        _window(user, datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 2, 1, tzinfo=UTC))
        user.pending_plan = "pro"
        user.pending_records_limit = 1000
        user_id = user.id
        db.commit()

    reconcile_quota_periods()

    with SyncSessionLocal() as db:
        refreshed = db.get(User, user_id)
        assert refreshed.plan == "pro"
        assert refreshed.records_limit == 1000
        assert refreshed.records_used == 0, "the new window starts at the new cap"
        assert refreshed.pending_plan is None
        assert refreshed.pending_records_limit is None


def test_reconciliation_rolls_over_all_ended_windows():
    """All eligible users roll over, not just one."""
    from src.workers.scheduler import reconcile_quota_periods

    ids = []
    with SyncSessionLocal() as db:
        for _ in range(3):
            u = _create_sync_user(db, records_used=100)
            _window(u, datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 2, 1, tzinfo=UTC))
            ids.append(u.id)
        db.commit()

    reconcile_quota_periods()

    with SyncSessionLocal() as db:
        for uid in ids:
            assert db.get(User, uid).records_used == 0


def test_skip_trace_reset_adopts_a_null_period_without_zeroing():
    """A NULL period is ADOPTED, never zeroed.

    Zeroing is the financially destructive direction — it hands out free quota
    and lets a user exceed their cap invisibly — so an unexpected NULL must cost
    us a stamped period, not the counter. (This is the exact shape of the
    production incident: every newly registered user had a NULL period and lost
    their entire month's usage on their first 00:05 UTC run.)

    Only skip-trace can still reach this state. The RECORD counter no longer has
    a nullable period at all — it is governed by quota_period_start/end, both NOT
    NULL with a server_default since migration 088 — so the arm that caused the
    incident is now structurally unreachable rather than merely guarded.
    """
    from sqlalchemy import text as _text

    from src.workers.scheduler import reset_skip_trace_usage

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        user.skip_trace_used_this_month = 777
        user_id = user.id
        db.commit()
        # The column is NOT NULL as of migration 086, so force the legacy shape
        # the way only pre-086 data could have been written.
        db.execute(
            _text("ALTER TABLE users ALTER COLUMN skip_trace_period_start DROP NOT NULL")
        )
        db.execute(
            _text("UPDATE users SET skip_trace_period_start = NULL WHERE id = :i"),
            {"i": user_id},
        )
        db.commit()

    try:
        reset_skip_trace_usage()

        with SyncSessionLocal() as db:
            refreshed = db.get(User, user_id)
            assert refreshed.skip_trace_used_this_month == 777, (
                "NULL period must not cost the counter"
            )
            assert refreshed.skip_trace_period_start == _current_month_start()
    finally:
        with SyncSessionLocal() as db:
            db.execute(
                _text(
                    "ALTER TABLE users ALTER COLUMN skip_trace_period_start SET NOT NULL"
                )
            )
            db.commit()


def test_skip_trace_reset_keys_on_its_OWN_period():
    """Skip-trace keys on skip_trace_period_start, and never touches records.

    It used to be gated on records_period_start, so drift between the two columns
    could reset Stripe-metered skip-trace usage early, or never. The two are now
    governed by entirely different mechanisms, which makes the separation
    structural: this task cannot reach the record counter at all.
    """
    from src.workers.scheduler import reset_skip_trace_usage

    now = datetime.now(UTC)
    with SyncSessionLocal() as db:
        user = _create_sync_user(db, records_used=10)
        _window(user, now - timedelta(days=1), now + timedelta(days=29))  # live
        user.skip_trace_used_this_month = 25
        user.skip_trace_period_start = datetime(2020, 1, 1, tzinfo=UTC)  # stale
        user_id = user.id
        db.commit()

    reset_skip_trace_usage()

    with SyncSessionLocal() as db:
        refreshed = db.get(User, user_id)
        assert refreshed.records_used == 10, "record quota is not this task's business"
        assert refreshed.skip_trace_used_this_month == 0, "its own period was stale"


# ─── Delivery: payment failed email ───────────────────────────────────────────

def test_send_payment_failed_email_soft_fails_gracefully():
    """With a fake RESEND_API_KEY, the function must not raise — only log."""
    from src.workers.delivery import _send_payment_failed_email
    # RESEND_API_KEY is set to "re_fake" in CI — call will fail silently
    _send_payment_failed_email("test@test.bridgeleads.io", attempt_count=1)
    # If we reach here without an exception the test passes


def test_deliver_job_email_soft_fails_gracefully(monkeypatch):
    """A PERMANENT send failure (e.g. invalid key) must not raise out of the
    task — a dropped delivery email never errors the (already-done) scrape job.
    Forced deterministically (no network) via a permanent Resend error."""
    import resend
    from resend import exceptions as resend_ex

    from src.workers import delivery

    def _raise_permanent(*_a, **_k):
        raise resend_ex.ValidationError(message="bad", error_type="validation_error", code=422)

    monkeypatch.setattr(resend.Emails, "send", _raise_permanent)
    # .apply() runs the task body eagerly (like test_webhook_ssrf).
    result = delivery.deliver_job_email.apply(kwargs={
        "job_id": str(uuid.uuid4()),
        "scraper_name": "Pierce County Probate",
        "record_count": 10,
        "download_url": "https://example.com/fake",
        "recipient_emails": ["test@test.bridgeleads.io"],
        "fmt": "csv",
    })
    assert result.successful()  # permanent failure → returned, not raised


def test_on_failure_terminalizes_stuck_non_terminal_job():
    """An uncaught exception in run_scrape_job leaves the job in a non-terminal
    status (e.g. 'enriching'). The crash cleanup must atomically fail the OWNED attempt
    (matching started_at) so it terminalizes with an error message instead of hanging
    until the watchdog's slow started_at fallback (the 2026-06-18 insertmanyvalues
    .rowcount crash failure mode)."""
    from src.workers.tasks import _fail_job_after_uncaught

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        started = datetime.now(UTC)
        job = Job(
            id=str(uuid.uuid4()),
            user_id=user.id,
            scraper_config_id=config.id,
            status="enriching",
            trigger="manual",
            started_at=started,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    # The crashed attempt owns the row (started_at matches what it stamped at claim).
    _fail_job_after_uncaught(job_id, "Job failed during processing.", expected_started_at=started)

    with SyncSessionLocal() as db:
        row = db.get(Job, job_id)
        assert row.status == "failed"
        assert row.error_message  # carries a human-readable reason
        assert row.finished_at is not None


def test_on_failure_soft_timeout_left_for_watchdog_retry():
    """Codex P2: a SoftTimeLimitExceeded is a recoverable timeout, not a crash. on_failure
    must NOT terminalize it — the watchdog re-queues long scrapes up to max_retries. The
    job stays non-terminal so the existing retry path is preserved."""
    from celery.exceptions import SoftTimeLimitExceeded

    from src.workers.tasks import run_scrape_job

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        started = datetime.now(UTC)
        job = Job(
            id=str(uuid.uuid4()),
            user_id=user.id,
            scraper_config_id=config.id,
            status="scraping",
            trigger="manual",
            started_at=started,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    # Even with a matching attempt token, a soft timeout must be left for the watchdog.
    task = run_scrape_job
    try:
        task.request.scrape_started_at = started
        task.on_failure(SoftTimeLimitExceeded(), "task-t", (job_id,), {}, None)
    finally:
        # Don't leak the stashed token into other tests sharing the task singleton.
        if hasattr(task.request, "scrape_started_at"):
            try:
                del task.request.scrape_started_at
            except Exception:
                task.request.scrape_started_at = None

    with SyncSessionLocal() as db:
        row = db.get(Job, job_id)
        assert row.status == "scraping"  # untouched — watchdog will retry
        assert row.error_message is None


def test_on_failure_leaves_already_billed_job_for_watchdog():
    """Codex P2: a crash AFTER billing committed (billing_applied_at set) but before the
    final 'done' must NOT be terminalized — the user is already charged and the watchdog
    re-run (billing CAS skips) drives it to 'done'. Failing it would leave a
    charged-but-failed job. Only not-yet-billed crashes terminalize."""
    from src.workers.tasks import _fail_job_after_uncaught

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        started = datetime.now(UTC)
        job = Job(
            id=str(uuid.uuid4()),
            user_id=user.id,
            scraper_config_id=config.id,
            status="enriching",
            trigger="manual",
            started_at=started,
            billed_count=5,
            billing_applied_at=datetime.now(UTC),  # already charged
        )
        db.add(job)
        db.commit()
        job_id = job.id

    _fail_job_after_uncaught(job_id, "post-billing transient crash", expected_started_at=started)

    with SyncSessionLocal() as db:
        row = db.get(Job, job_id)
        assert row.status == "enriching"  # left for the watchdog to complete
        assert row.error_message is None


def test_on_failure_without_attempt_token_is_noop():
    """Codex P2: a task that crashed BEFORE winning the pending->queued claim (or a stale
    duplicate delivery) has no started_at token. It never owned the job, so cleanup must
    skip rather than risk failing another live attempt. on_failure passes None when the
    request never recorded scrape_started_at."""
    from src.workers.tasks import run_scrape_job

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        job = Job(
            id=str(uuid.uuid4()),
            user_id=user.id,
            scraper_config_id=config.id,
            status="scraping",
            trigger="manual",
            started_at=datetime.now(UTC),
        )
        db.add(job)
        db.commit()
        job_id = job.id

    # Direct on_failure call: self.request carries no scrape_started_at -> token is None.
    run_scrape_job.on_failure(RuntimeError("pre-claim boom"), "task-1", (job_id,), {}, None)

    with SyncSessionLocal() as db:
        row = db.get(Job, job_id)
        assert row.status == "scraping"  # untouched — no ownership token
        assert row.error_message is None


def test_on_failure_leaves_already_terminal_job_untouched():
    """If the job genuinely finished before an unrelated late crash, cleanup must NOT
    overwrite the terminal status (the atomic UPDATE excludes terminal rows) even when
    the attempt token matches."""
    from src.workers.tasks import _fail_job_after_uncaught

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        started = datetime.now(UTC)
        job = Job(
            id=str(uuid.uuid4()),
            user_id=user.id,
            scraper_config_id=config.id,
            status="done",
            trigger="manual",
            record_count=7,
            started_at=started,
            finished_at=datetime.now(UTC),
        )
        db.add(job)
        db.commit()
        job_id = job.id

    _fail_job_after_uncaught(job_id, "late boom", expected_started_at=started)

    with SyncSessionLocal() as db:
        row = db.get(Job, job_id)
        assert row.status == "done"  # never flipped to failed
        assert row.record_count == 7


def test_on_failure_is_attempt_scoped_skips_requeued_attempt():
    """Codex P2: an old attempt's late on_failure must NOT clobber a row that was
    re-queued/re-claimed for a newer attempt. The watchdog re-queue nulls started_at and
    a replacement claim stamps a fresh one, so when the crashed attempt's started_at no
    longer matches the row, cleanup must skip (preserving watchdog retry recovery)."""
    from src.workers.tasks import _fail_job_after_uncaught

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        current_started = datetime.now(UTC)
        job = Job(
            id=str(uuid.uuid4()),
            user_id=user.id,
            scraper_config_id=config.id,
            status="scraping",  # a live NEWER attempt
            trigger="manual",
            started_at=current_started,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    # The crashed OLD attempt had a different (earlier) started_at.
    stale_started = current_started - timedelta(minutes=20)
    _fail_job_after_uncaught(job_id, "old attempt crashed", expected_started_at=stale_started)

    with SyncSessionLocal() as db:
        row = db.get(Job, job_id)
        assert row.status == "scraping"  # live newer attempt untouched
        assert row.error_message is None


# ─── Job liveness: the 2026-09-09 stranded-scrape incident (job 9c8b7259) ────
# A Railway worker redeploy stopped the container ~13s into a scrape. A container
# stop raises no Python exception, so Task.on_failure never ran and the row stayed
# 'scraping' with no error and no finished_at. Recovery is owned by the watchdog,
# whose fast branch keys on last_heartbeat_at — which was permanently NULL because
# the HeartbeatThread had been disabled since the 2026-06-18 pool-deadlock
# rollback. The job therefore showed as running for the full 70-minute fallback.
# These tests pin the three things that fix and keep fixing that.

def test_heartbeat_writes_on_the_isolated_engine_and_never_the_work_pool():
    """The heartbeat MUST NOT touch the worker's pool_size=2 work engine.

    Sharing that pool is what deadlocked every scrape at the insert phase on
    2026-06-18 and got the heartbeat disabled for months, which in turn left
    last_heartbeat_at NULL and the watchdog's fast branch dead.

    Asserting the engine's poolclass alone would still pass if _write_heartbeat
    were switched back to system_sync_session (Codex), so this OBSERVES which
    engine actually serves the write, via connection events on both.
    """
    from sqlalchemy import event
    from sqlalchemy.pool import NullPool

    from src.db.session import heartbeat_engine, sync_engine
    from src.workers.tasks_helpers.status import _HB_ALIVE, _write_heartbeat

    assert heartbeat_engine is not sync_engine
    assert isinstance(heartbeat_engine.pool, NullPool)

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        started = datetime.now(UTC) - timedelta(minutes=3)
        job = _create_stuck_job(db, user.id, config.id, minutes_ago=3)
        job.started_at = started
        job_id = job.id
        db.commit()

    seen = {"heartbeat": 0, "work": 0}

    def _on_hb(*_a, **_kw):
        seen["heartbeat"] += 1

    def _on_work(*_a, **_kw):
        seen["work"] += 1

    event.listen(heartbeat_engine, "connect", _on_hb)
    event.listen(sync_engine, "connect", _on_work)
    try:
        # Drain the work pool so a fresh checkout there would have to CONNECT,
        # making an accidental use of it observable rather than pool-cached.
        sync_engine.dispose()
        seen["heartbeat"] = seen["work"] = 0
        assert _write_heartbeat(job_id, started) == _HB_ALIVE
    finally:
        event.remove(heartbeat_engine, "connect", _on_hb)
        event.remove(sync_engine, "connect", _on_work)

    assert seen["heartbeat"] >= 1, "the heartbeat did not use its own engine"
    assert seen["work"] == 0, "the heartbeat opened a connection on the WORK engine"

    with SyncSessionLocal() as db:
        assert db.get(Job, job_id).last_heartbeat_at is not None


def test_run_scrape_job_still_starts_the_heartbeat():
    """The heartbeat start was disabled by COMMENTING IT OUT for three months and
    nothing failed: last_heartbeat_at just went NULL everywhere and the watchdog
    silently lost its 15-minute branch. A behavioural test cannot reach that call
    (it sits deep inside a task needing Redis and a browser), so pin the one thing
    that actually regressed: it must be live code, not a comment."""
    import inspect

    from src.workers.tasks import run_scrape_job

    body = inspect.getsource(run_scrape_job.__wrapped__)
    live = [
        ln for ln in body.splitlines()
        if "_hb.start(" in ln and not ln.strip().startswith("#")
    ]
    assert live, "run_scrape_job no longer starts the HeartbeatThread"


def test_production_claim_stamps_a_fresh_heartbeat_and_is_at_most_once():
    """The retry-storm guard, asserted against the PRODUCTION claim helper.

    run_scrape_job calls claim_job_for_attempt; a test that re-typed the UPDATE
    would stay green if the heartbeat stamp were deleted from the real claim
    (Codex), so this calls the same function the worker calls.
    """
    from src.workers.tasks_helpers.status import claim_job_for_attempt

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        job = Job(
            id=str(uuid.uuid4()),
            user_id=user.id,
            scraper_config_id=config.id,
            status="pending",
            trigger="manual",
            retry_count=1,
            # Worst case: a stale stamp survived onto a pending row.
            last_heartbeat_at=datetime.now(UTC) - timedelta(minutes=45),
        )
        db.add(job)
        db.commit()
        job_id = job.id

    with SyncSessionLocal() as db:
        claimed_at = claim_job_for_attempt(db, job_id)
    assert claimed_at is not None                         # first delivery wins

    with SyncSessionLocal() as db:
        assert claim_job_for_attempt(db, job_id) is None  # duplicate claims nothing

    with SyncSessionLocal() as db:
        row = db.get(Job, job_id)
        assert row.status == "queued"
        assert row.started_at is not None
        # The stale 45-minute-old stamp must be GONE, replaced by this attempt's.
        assert row.last_heartbeat_at is not None
        assert row.last_heartbeat_at >= row.started_at - timedelta(seconds=1)
        assert row.last_heartbeat_at > datetime.now(UTC) - timedelta(minutes=1)


def test_a_freshly_claimed_attempt_survives_the_next_watchdog_tick():
    """End to end: a job re-queued carrying a dead attempt's stale heartbeat, then
    re-claimed, must NOT be re-queued again by the very next watchdog sweep.
    Without the claim-time stamp this burns the retry budget until a healthy job
    is permanently failed."""
    from src.workers.scheduler import watchdog_stuck_jobs
    from src.workers.tasks_helpers.status import claim_job_for_attempt

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        job = Job(
            id=str(uuid.uuid4()),
            user_id=user.id,
            scraper_config_id=config.id,
            status="pending",
            trigger="manual",
            retry_count=1,
            last_heartbeat_at=datetime.now(UTC) - timedelta(minutes=45),
        )
        db.add(job)
        db.commit()
        job_id = job.id

    with SyncSessionLocal() as db:
        assert claim_job_for_attempt(db, job_id) is not None

    watchdog_stuck_jobs()

    with SyncSessionLocal() as db:
        row = db.get(Job, job_id)
        assert row.status == "queued"     # still owned by the live attempt
        assert row.retry_count == 1       # no retry burned


def test_watchdog_requeue_clears_the_dead_attempts_heartbeat():
    """A re-queued job is 'pending' and owned by nobody, so it must carry NO
    liveness stamp from the attempt that died.

    Leaving the dead attempt's last_heartbeat_at in place is a lie about a worker
    that is gone, and it is what would let the NEXT attempt inherit an already
    stale value.
    """
    from src.workers.scheduler import watchdog_stuck_jobs

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        job = _create_stuck_job(db, user.id, config.id, minutes_ago=30)
        job.last_heartbeat_at = datetime.now(UTC) - timedelta(minutes=20)  # stale
        job_id = job.id
        db.commit()

    watchdog_stuck_jobs()

    with SyncSessionLocal() as db:
        refreshed = db.get(Job, job_id)
        assert refreshed.status == "pending"
        assert refreshed.retry_count == 1
        assert refreshed.last_heartbeat_at is None


def test_transient_retry_reset_clears_the_heartbeat_and_keeps_billing_safe():
    """_retry_scrape_job hands the row back as a clean, un-started, un-beaten
    'pending' attempt, and refuses outright once billing has been applied."""
    from src.workers.tasks_helpers.status import _retry_scrape_job

    with SyncSessionLocal() as db:
        user = _create_sync_user(db)
        config = _create_sync_config(db, user.id)
        started = datetime.now(UTC) - timedelta(minutes=1)
        job = _create_stuck_job(db, user.id, config.id, minutes_ago=1)
        job.started_at = started
        job.last_heartbeat_at = datetime.now(UTC)
        job.page_current, job.page_total, job.record_count = 1, 2, 38
        job_id = job.id
        db.commit()

        countdown = _retry_scrape_job(
            db, job, job_id, started, max_retries=2, backoffs=(300, 1200),
        )
        assert countdown is not None
        assert 300 <= countdown <= 360  # base + bounded jitter

    with SyncSessionLocal() as db:
        refreshed = db.get(Job, job_id)
        assert refreshed.status == "pending"
        assert refreshed.retry_count == 1
        assert refreshed.started_at is None
        assert refreshed.last_heartbeat_at is None
        assert (refreshed.page_current, refreshed.page_total, refreshed.record_count) == (0, 0, 0)

    # Once billing has landed, a transient retry must never re-queue the job.
    with SyncSessionLocal() as db:
        job = db.get(Job, job_id)
        billed_started = datetime.now(UTC)
        job.status = "enriching"
        job.started_at = billed_started
        job.billing_applied_at = datetime.now(UTC)
        job.billed_count = 38
        db.commit()
        assert _retry_scrape_job(
            db, job, job_id, billed_started, max_retries=2, backoffs=(300, 1200),
        ) is None

    with SyncSessionLocal() as db:
        assert db.get(Job, job_id).retry_count == 1  # unchanged, no extra retry
