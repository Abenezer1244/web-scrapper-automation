"""finalize_billing_and_done: billing and the done transition, as production runs them.

Real DB, real Redis (the test db index). Each test builds a job that an attempt is
finalizing and calls the production helper with that attempt's token, exactly as
run_scrape_job does.
"""
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig, User
from src.db.session import SyncSessionLocal
from src.workers.tasks_helpers.finalize import FinalizeKind, finalize_billing_and_done

ADDR = "5006 61ST STREET CT E"
A_TOKEN = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _make_job(user_id, config_id, *, rows: int, **kw) -> str:
    job_id = str(uuid.uuid4())
    kw.setdefault("status", "enriching")
    kw.setdefault("started_at", A_TOKEN)
    with SyncSessionLocal() as s:
        s.add(Job(id=job_id, user_id=user_id, scraper_config_id=config_id,
                  trigger="manual", records_found=rows, **kw))
        s.flush()
        for _ in range(rows):
            s.add(Result(id=str(uuid.uuid4()), job_id=job_id, user_id=user_id,
                         property_address=ADDR, is_duplicate=False))
        s.commit()
    return job_id


def _finalize(job_id, redis_client, *, token=A_TOKEN):
    with SyncSessionLocal() as db:
        job = db.get(Job, job_id)
        user = db.get(User, job.user_id)
        config = db.get(ScraperConfig, job.scraper_config_id)
        return finalize_billing_and_done(
            db, redis_client, job=job, user=user, config=config, job_id=job_id,
            attempt_started_at=token, object_key=f"exports/{job.user_id}/{job_id}/leads.csv",
            boot_user_id=str(job.user_id),
        )


def _row(job_id):
    with SyncSessionLocal() as s:
        return s.execute(text(
            "SELECT j.status, j.billed_count, j.billing_applied_at, j.record_count, "
            "u.records_used FROM jobs j JOIN users u ON u.id = j.user_id WHERE j.id = :j"
        ), {"j": job_id}).one()


@pytest.fixture
def ids(starter_user, scraper_config):
    return starter_user.id, scraper_config.id


def test_an_owned_attempt_bills_once_and_completes(ids, redis_client):
    job_id = _make_job(*ids, rows=2)

    outcome = _finalize(job_id, redis_client)

    assert (outcome.kind, outcome.display_count) == (FinalizeKind.DONE, 2)
    status, billed, billed_at, record_count, used = _row(job_id)
    assert (status, billed, record_count, used) == ("done", 2, 2, 2)
    assert billed_at is not None


def test_a_rerun_of_a_billed_job_reports_the_charge_and_does_not_bill_again(
    ids, redis_client,
):
    job_id = _make_job(*ids, rows=2, billed_count=5,
                       billing_applied_at=datetime(2026, 9, 28, 11, 0, tzinfo=UTC))

    outcome = _finalize(job_id, redis_client)

    assert (outcome.kind, outcome.display_count) == (FinalizeKind.DONE, 5)
    status, billed, _, record_count, used = _row(job_id)
    assert (status, billed, record_count, used) == ("done", 5, 5, 0)


@pytest.mark.parametrize("terminal", ["cancelled", "failed", "done"])
def test_a_job_terminal_before_finalization_bills_nothing(ids, redis_client, terminal):
    job_id = _make_job(*ids, rows=2, status=terminal)

    outcome = _finalize(job_id, redis_client)

    assert outcome.kind is FinalizeKind.ALREADY_TERMINAL
    status, billed, billed_at, _, used = _row(job_id)
    assert (status, billed, billed_at, used) == (terminal, 0, None, 0)
