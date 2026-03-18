"""Tests for job CRUD, record limit enforcement, cancel rules, and SSE log replay."""
import json
import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import src.db.session as _db_session
from src.db.models import Job, JobLog, ScraperConfig, User

# ─── List jobs ────────────────────────────────────────────────────────────────

async def test_list_jobs_empty(client: AsyncClient, starter_user: User, starter_token: str):
    resp = await client.get("/jobs", headers={"Authorization": f"Bearer {starter_token}"})
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_jobs_returns_own_jobs_only(
    client: AsyncClient,
    db: AsyncSession,
    starter_user: User,
    starter_token: str,
    pending_job: Job,
):
    resp = await client.get("/jobs", headers={"Authorization": f"Bearer {starter_token}"})
    assert resp.status_code == 200
    job_ids = [j["id"] for j in resp.json()]
    assert pending_job.id in job_ids


# ─── Get single job ───────────────────────────────────────────────────────────

async def test_get_job(
    client: AsyncClient,
    starter_user: User,
    starter_token: str,
    pending_job: Job,
):
    resp = await client.get(
        f"/jobs/{pending_job.id}",
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == pending_job.id
    assert resp.json()["status"] == "pending"


async def test_get_job_not_found(client: AsyncClient, starter_token: str):
    resp = await client.get(
        f"/jobs/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 404


async def test_get_job_wrong_user_returns_404(
    client: AsyncClient,
    db: AsyncSession,
    business_user: User,
    business_token: str,
    pending_job: Job,
):
    # pending_job belongs to starter_user; business_user must not see it
    resp = await client.get(
        f"/jobs/{pending_job.id}",
        headers={"Authorization": f"Bearer {business_token}"},
    )
    assert resp.status_code == 404


# ─── Create job ───────────────────────────────────────────────────────────────

async def test_create_job(
    client: AsyncClient,
    starter_user: User,
    starter_token: str,
    scraper_config: ScraperConfig,
):
    resp = await client.post(
        "/jobs",
        json={"scraper_config_id": scraper_config.id, "trigger": "manual"},
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "pending"
    assert data["scraper_config_id"] == scraper_config.id


async def test_create_job_invalid_scraper_id(client: AsyncClient, starter_token: str):
    resp = await client.post(
        "/jobs",
        json={"scraper_config_id": str(uuid.uuid4()), "trigger": "manual"},
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 404


# ─── Record limit enforcement ─────────────────────────────────────────────────

async def test_create_job_blocked_when_limit_reached(
    client: AsyncClient,
    starter_user: User,
    starter_token: str,
    scraper_config: ScraperConfig,
):
    # Max out the quota using an inline session to avoid cross-loop db fixture issues
    async with _db_session.AsyncSessionLocal() as s:
        result = await s.execute(select(User).where(User.id == starter_user.id))
        u = result.scalar_one()
        u.records_used = u.records_limit
        await s.commit()

    resp = await client.post(
        "/jobs",
        json={"scraper_config_id": scraper_config.id, "trigger": "manual"},
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 402
    assert "limit" in resp.json()["detail"].lower()


async def test_create_job_allowed_when_under_limit(
    client: AsyncClient,
    starter_user: User,
    starter_token: str,
    scraper_config: ScraperConfig,
):
    async with _db_session.AsyncSessionLocal() as s:
        result = await s.execute(select(User).where(User.id == starter_user.id))
        u = result.scalar_one()
        u.records_used = u.records_limit - 1
        await s.commit()

    resp = await client.post(
        "/jobs",
        json={"scraper_config_id": scraper_config.id, "trigger": "manual"},
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 201


async def test_unlimited_plan_never_blocked(
    client: AsyncClient,
    business_user: User,
    business_token: str,
):
    config_id = str(uuid.uuid4())
    async with _db_session.AsyncSessionLocal() as s:
        s.add(ScraperConfig(
            id=config_id,
            user_id=business_user.id,
            name="Business Test Config",
            county="pierce",
            state="WA",
            record_type="probate",
            fields=[],
            enrichment=[],
            schedule={"frequency": "manual"},
            deliver={"format": "csv", "emails": []},
        ))
        result = await s.execute(select(User).where(User.id == business_user.id))
        u = result.scalar_one()
        # Set records_used very high — business plan (-1) should never block
        u.records_limit = -1
        u.records_used = 999999
        await s.commit()

    resp = await client.post(
        "/jobs",
        json={"scraper_config_id": config_id, "trigger": "manual"},
        headers={"Authorization": f"Bearer {business_token}"},
    )
    assert resp.status_code == 201


# ─── Cancel job ───────────────────────────────────────────────────────────────

async def test_cancel_pending_job(
    client: AsyncClient,
    starter_token: str,
    pending_job: Job,
):
    resp = await client.delete(
        f"/jobs/{pending_job.id}",
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 204


async def test_cancel_done_job_returns_400(
    client: AsyncClient,
    starter_user: User,
    starter_token: str,
    scraper_config: ScraperConfig,
):
    job_id = str(uuid.uuid4())
    async with _db_session.AsyncSessionLocal() as s:
        s.add(Job(
            id=job_id,
            user_id=starter_user.id,
            scraper_config_id=scraper_config.id,
            status="done",
            trigger="manual",
        ))
        await s.commit()

    resp = await client.delete(
        f"/jobs/{job_id}",
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 400
    assert "cancel" in resp.json()["detail"].lower()


async def test_cancel_failed_job_returns_400(
    client: AsyncClient,
    starter_user: User,
    starter_token: str,
    scraper_config: ScraperConfig,
):
    job_id = str(uuid.uuid4())
    async with _db_session.AsyncSessionLocal() as s:
        s.add(Job(
            id=job_id,
            user_id=starter_user.id,
            scraper_config_id=scraper_config.id,
            status="failed",
            trigger="manual",
        ))
        await s.commit()

    resp = await client.delete(
        f"/jobs/{job_id}",
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 400


# ─── SSE log replay ───────────────────────────────────────────────────────────

async def test_sse_replays_existing_logs(
    client: AsyncClient,
    starter_user: User,
    starter_token: str,
    pending_job: Job,
):
    # Seed log entries and mark job done using an inline session
    async with _db_session.AsyncSessionLocal() as s:
        for msg in ["Job queued — Pierce County", "Probing county portal..."]:
            s.add(JobLog(
                id=str(uuid.uuid4()),
                job_id=pending_job.id,
                level="info",
                message=msg,
            ))
        result = await s.execute(select(Job).where(Job.id == pending_job.id))
        job = result.scalar_one()
        job.status = "done"
        await s.commit()

    resp = await client.get(
        f"/jobs/{pending_job.id}/logs",
        headers={
            "Authorization": f"Bearer {starter_token}",
            "Accept": "text/event-stream",
        },
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]

    # Parse SSE lines
    lines = [line for line in resp.text.split("\n") if line.startswith("data: ")]
    messages = [json.loads(line[len("data: "):]) for line in lines]

    log_messages = [m["message"] for m in messages if "message" in m]
    assert "Job queued — Pierce County" in log_messages
    assert "Probing county portal..." in log_messages


async def test_sse_returns_404_for_unknown_job(client: AsyncClient, starter_token: str):
    resp = await client.get(
        f"/jobs/{uuid.uuid4()}/logs",
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 404
