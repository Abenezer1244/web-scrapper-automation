"""Auto skip trace never pays for a code violation the city already settled.

A complaint Seattle SDCI closed as "Completed", or filed as an "Open Duplicate" of
another complaint, is not worth a paid Tracerfy lookup (owner decision 2026-09-13).
Real DB and real Redis; nothing is sent to Tracerfy here, the enqueue only writes
pending_skip_trace_rows.
"""
from __future__ import annotations

import os
import random
import uuid

import pytest
import redis as sync_redis
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig, User

pytestmark = pytest.mark.asyncio


async def _job(db, user: User, record_type: str) -> str:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="Settled Complaint Config",
        county="king", state="WA", record_type=record_type,
        fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []}, skip_trace_enabled=True,
    )
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id,
               status="enriching", trigger="manual"))
    await db.commit()
    return job_id


async def _lead(db, user: User, job_id: str, complaint_status: str | None) -> str:
    rid = str(uuid.uuid4())
    ed = {"source": "seattle_sdci_code_violations", "record_number": f"{random.randint(1, 999999):06d}-26CP"}
    if complaint_status is not None:
        ed["status"] = complaint_status
    db.add(Result(
        id=rid, user_id=user.id, job_id=job_id, party_name="DOE JANE",
        property_address=f"{random.randint(100, 99999)} MAIN ST, SEATTLE, WA 98101",
        enrichment_data=ed, skip_trace_status="not_attempted", is_duplicate=False,
    ))
    await db.commit()
    return rid


def _enqueue(job_id: str) -> None:
    from src.db.session import SyncSessionLocal
    from src.workers.tasks_helpers.enrich import _enqueue_skip_trace_rows

    r = sync_redis.Redis.from_url(os.environ["REDIS_URL"])
    with SyncSessionLocal() as s:
        job = s.get(Job, job_id)
        config = s.get(ScraperConfig, job.scraper_config_id)
        _enqueue_skip_trace_rows(s, job, r, job_id, config)


async def _queued_for(db, job_id: str) -> set[str]:
    return {str(x) for x in (await db.execute(
        text("SELECT result_id FROM pending_skip_trace_rows WHERE job_id = :j"), {"j": job_id}
    )).scalars()}


@pytest.fixture
def skip_trace_on(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")


async def test_completed_and_duplicate_complaints_are_not_queued(db, business_user, skip_trace_on):
    job_id = await _job(db, business_user, "code_violation")
    completed = await _lead(db, business_user, job_id, "Completed")
    duplicate = await _lead(db, business_user, job_id, "Open Duplicate")
    open_case = await _lead(db, business_user, job_id, "Under Investigation")
    closed = await _lead(db, business_user, job_id, "Closed")
    lowercase = await _lead(db, business_user, job_id, "completed")
    no_status = await _lead(db, business_user, job_id, None)

    _enqueue(job_id)

    queued = await _queued_for(db, job_id)
    assert completed not in queued and duplicate not in queued
    # Exact values only: "Closed", other casing and a missing status are still traced.
    assert {open_case, closed, lowercase, no_status} <= queued
    status = (await db.execute(text("SELECT skip_trace_status FROM results WHERE id = ANY(:ids)"),
                               {"ids": [completed, duplicate]})).scalars().all()
    assert status == ["not_attempted", "not_attempted"]


async def test_the_gate_is_scoped_to_code_violations(db, business_user, skip_trace_on):
    job_id = await _job(db, business_user, "tax_delinquent")
    lead = await _lead(db, business_user, job_id, "Completed")

    _enqueue(job_id)

    assert lead in await _queued_for(db, job_id)
