"""Tests for job CRUD, record limit enforcement, cancel rules, and SSE log replay."""
import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

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
    db: AsyncSession,
    starter_user: User,
    starter_token: str,
    scraper_config: ScraperConfig,
):
    # Max out the quota
    starter_user.records_used = starter_user.records_limit
    await db.commit()

    resp = await client.post(
        "/jobs",
        json={"scraper_config_id": scraper_config.id, "trigger": "manual"},
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 402
    assert "limit" in resp.json()["detail"].lower()


async def test_create_job_allowed_when_under_limit(
    client: AsyncClient,
    db: AsyncSession,
    starter_user: User,
    starter_token: str,
    scraper_config: ScraperConfig,
):
    starter_user.records_used = starter_user.records_limit - 1
    await db.commit()

    resp = await client.post(
        "/jobs",
        json={"scraper_config_id": scraper_config.id, "trigger": "manual"},
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 201


async def test_unlimited_plan_never_blocked(
    client: AsyncClient,
    db: AsyncSession,
    business_user: User,
    business_token: str,
):
    # Create a scraper config for the business user
    config = ScraperConfig(
        id=str(uuid.uuid4()),
        user_id=business_user.id,
        name="Business Test Config",
        county="pierce",
        state="WA",
        record_type="probate",
        fields=[],
        enrichment=[],
        schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    # Set records_used very high — business plan (-1) should never block
    business_user.records_limit = -1
    business_user.records_used = 999999
    await db.commit()

    resp = await client.post(
        "/jobs",
        json={"scraper_config_id": config.id, "trigger": "manual"},
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
    db: AsyncSession,
    starter_user: User,
    starter_token: str,
    scraper_config: ScraperConfig,
):
    done_job = Job(
        id=str(uuid.uuid4()),
        user_id=starter_user.id,
        scraper_config_id=scraper_config.id,
        status="done",
        trigger="manual",
    )
    db.add(done_job)
    await db.commit()

    resp = await client.delete(
        f"/jobs/{done_job.id}",
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 400
    assert "cancel" in resp.json()["detail"].lower()


async def test_cancel_failed_job_returns_400(
    client: AsyncClient,
    db: AsyncSession,
    starter_user: User,
    starter_token: str,
    scraper_config: ScraperConfig,
):
    failed_job = Job(
        id=str(uuid.uuid4()),
        user_id=starter_user.id,
        scraper_config_id=scraper_config.id,
        status="failed",
        trigger="manual",
    )
    db.add(failed_job)
    await db.commit()

    resp = await client.delete(
        f"/jobs/{failed_job.id}",
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 400


# ─── SSE log replay ───────────────────────────────────────────────────────────

async def test_sse_replays_existing_logs(
    client: AsyncClient,
    db: AsyncSession,
    starter_user: User,
    starter_token: str,
    pending_job: Job,
):
    # Seed two log entries for the job
    for msg in ["Job queued — Pierce County", "Probing county portal..."]:
        db.add(JobLog(
            id=str(uuid.uuid4()),
            job_id=pending_job.id,
            level="info",
            message=msg,
        ))

    # Mark job done so SSE terminates after replay
    pending_job.status = "done"
    await db.commit()

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
