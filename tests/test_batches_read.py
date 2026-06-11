"""Piece 2 Phase 2A.4 — batch read + download endpoints.

Pure tests lock the response-schema shape + router wiring. DB-backed tests
(client + real Postgres) verify the list/detail/download contract and tenant
isolation — ScraperBatch / BatchRun are system-written + NOT RLS-granted, so the
explicit user_id filter is the only tenant boundary and MUST be proven. These
run in CI like the rest of the suite.
"""
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import (
    BatchChildSummary,
    BatchDetailResponse,
    BatchDownloadResponse,
    BatchSummaryResponse,
)
from src.db.models import BatchRun, Job, ScraperBatch, ScraperConfig, User


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ─── Pure: schema shape + router wiring ──────────────────────────────────────

class TestSchemas:
    def test_summary_defaults(self):
        s = BatchSummaryResponse(id="b", state="WA", created_at=datetime.now(UTC))
        assert s.run_status == "pending"
        assert s.child_count == 0
        assert s.combined_export_ready is False
        assert s.completed_at is None

    def test_detail_inherits_summary_and_adds_children(self):
        d = BatchDetailResponse(id="b", state="WA", created_at=datetime.now(UTC))
        assert d.children == []
        assert d.failed_children is None
        assert d.run_status == "pending"  # inherited from BatchSummaryResponse

    def test_child_summary_defaults(self):
        c = BatchChildSummary(config_id="c", county="king", record_type="probate")
        assert c.job_id is None
        assert c.status == "pending"
        assert c.record_count == 0

    def test_download_response(self):
        assert BatchDownloadResponse(url="https://r2/x").url == "https://r2/x"


def test_read_routes_registered():
    from src.api import batches_router

    paths = {getattr(rt, "path", None) for rt in batches_router.routes}
    assert "/batches" in paths
    assert "/batches/{batch_id}" in paths
    assert "/batches/{batch_id}/download" in paths


# ─── DB-backed: a finished batch owned by starter_user ───────────────────────

@pytest_asyncio.fixture
async def starter_batch(db: AsyncSession, starter_user: User) -> SimpleNamespace:
    """A done batch: 1 child (king/probate, job done, 42 records) + a done run
    with a combined export key. Returns plain-string ids (no expired-attr access)."""
    batch = ScraperBatch(
        id=str(uuid.uuid4()),
        user_id=starter_user.id,
        name="My Batch",
        state="WA",
        fields=["party_name"],
        enrichment=[],
        schedule={},
        deliver={"emails": []},
        status="active",
    )
    db.add(batch)
    # Flush parents before the FK-dependent rows: batch_runs/scraper_configs carry
    # a composite FK (batch_id, user_id) -> scraper_batches, so the batch row must
    # exist in the txn first (in prod these are separate transactions).
    await db.flush()
    cfg = ScraperConfig(
        id=str(uuid.uuid4()),
        user_id=starter_user.id,
        batch_id=batch.id,
        name="King probate (batch)",
        county="king",
        state="WA",
        record_type="probate",
        fields=["party_name"],
        enrichment=[],
        schedule={},
        deliver={},
    )
    db.add(cfg)
    job = Job(
        id=str(uuid.uuid4()),
        user_id=starter_user.id,
        scraper_config_id=cfg.id,
        status="done",
        trigger="batch",
        record_count=42,
    )
    db.add(job)
    await db.flush()  # job must exist before the run references its id
    run = BatchRun(
        id=str(uuid.uuid4()),
        batch_id=batch.id,
        user_id=starter_user.id,
        status="done",
        child_job_ids=[job.id],
        combined_export_key=f"exports/{starter_user.id}/batch/{uuid.uuid4()}/combined.csv",
    )
    db.add(run)
    await db.commit()
    return SimpleNamespace(batch_id=batch.id, config_id=cfg.id, job_id=job.id)


# ─── GET /batches ─────────────────────────────────────────────────────────────

async def test_list_empty(client: AsyncClient, starter_user: User, starter_token: str):
    resp = await client.get("/batches", headers=_auth(starter_token))
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_returns_own_batch(
    client: AsyncClient, starter_token: str, starter_batch: SimpleNamespace
):
    resp = await client.get("/batches", headers=_auth(starter_token))
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    item = items[0]
    assert item["id"] == starter_batch.batch_id
    assert item["run_status"] == "done"
    assert item["child_count"] == 1
    assert item["combined_export_ready"] is True


async def test_list_tenant_isolation(
    client: AsyncClient, business_token: str, starter_batch: SimpleNamespace
):
    # business_user must not see starter_user's batch.
    resp = await client.get("/batches", headers=_auth(business_token))
    assert resp.status_code == 200
    assert resp.json() == []


# ─── GET /batches/{id} ────────────────────────────────────────────────────────

async def test_detail_per_child_summary(
    client: AsyncClient, starter_token: str, starter_batch: SimpleNamespace
):
    resp = await client.get(
        f"/batches/{starter_batch.batch_id}", headers=_auth(starter_token)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_status"] == "done"
    assert body["combined_export_ready"] is True
    assert len(body["children"]) == 1
    child = body["children"][0]
    assert child["county"] == "king"
    assert child["record_type"] == "probate"
    assert child["status"] == "done"
    assert child["record_count"] == 42
    assert child["job_id"] == starter_batch.job_id


async def test_detail_not_found(client: AsyncClient, starter_token: str):
    resp = await client.get(f"/batches/{uuid.uuid4()}", headers=_auth(starter_token))
    assert resp.status_code == 404


async def test_detail_tenant_isolation(
    client: AsyncClient, business_token: str, starter_batch: SimpleNamespace
):
    resp = await client.get(
        f"/batches/{starter_batch.batch_id}", headers=_auth(business_token)
    )
    assert resp.status_code == 404


# ─── GET /batches/{id}/download ───────────────────────────────────────────────

async def test_download_ready_branch_not_404(
    client: AsyncClient, starter_token: str, starter_batch: SimpleNamespace
):
    # combined_export_key IS set -> we take the presign branch, never 'not ready'.
    # Presign is 200 (R2 configured) or 503 (not) — both prove the key was found.
    resp = await client.get(
        f"/batches/{starter_batch.batch_id}/download", headers=_auth(starter_token)
    )
    assert resp.status_code in (200, 503)


async def test_download_not_ready_404(
    client: AsyncClient, db: AsyncSession, starter_user: User, starter_token: str
):
    # A batch whose run has NO combined_export_key yet -> 404 not ready.
    batch = ScraperBatch(
        id=str(uuid.uuid4()),
        user_id=starter_user.id,
        name="Running batch",
        state="WA",
        fields=["party_name"],
        enrichment=[],
        schedule={},
        deliver={},
        status="active",
    )
    db.add(batch)
    await db.flush()  # batch must exist before the run's composite FK references it
    run = BatchRun(
        id=str(uuid.uuid4()),
        batch_id=batch.id,
        user_id=starter_user.id,
        status="running",
        child_job_ids=[],
        combined_export_key=None,
    )
    db.add(run)
    await db.commit()
    resp = await client.get(f"/batches/{batch.id}/download", headers=_auth(starter_token))
    assert resp.status_code == 404


async def test_download_tenant_isolation(
    client: AsyncClient, business_token: str, starter_batch: SimpleNamespace
):
    resp = await client.get(
        f"/batches/{starter_batch.batch_id}/download", headers=_auth(business_token)
    )
    assert resp.status_code == 404
