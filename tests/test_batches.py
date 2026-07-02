"""Piece 2 Phase 2A.2 — batch create request validation + caps + router wiring.

Pure tests (no DB). The fan-out / gating SQL paths are exercised in CI.
"""
import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import BatchCreateRequest
from src.config.constants import (
    BATCH_HARD_CEILING,
    BATCH_MAX_COMBINATIONS,
    BATCH_PLANS,
    Plan,
)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestBatchCreateRequest:
    def test_dedupes_and_lowercases(self):
        r = BatchCreateRequest(
            state="wa",
            counties=["King", "king", " Pierce "],
            record_types=["PROBATE", "probate"],
        )
        assert r.counties == ["king", "pierce"]
        assert r.record_types == ["probate"]
        assert r.state == "WA"

    def test_empty_counties_rejected(self):
        with pytest.raises(ValidationError):
            BatchCreateRequest(state="WA", counties=[], record_types=["probate"])

    def test_bad_state_rejected(self):
        with pytest.raises(ValidationError):
            BatchCreateRequest(state="WAA", counties=["king"], record_types=["probate"])


class TestBatchCaps:
    def test_pro_below_business(self):
        assert BATCH_MAX_COMBINATIONS[Plan.PRO.value] < BATCH_MAX_COMBINATIONS[Plan.BUSINESS.value]

    def test_gate_excludes_free_starter_includes_pro(self):
        assert Plan.STARTER.value not in BATCH_PLANS
        assert Plan.PRO.value in BATCH_PLANS
        assert Plan.BUSINESS.value in BATCH_PLANS

    def test_caps_within_hard_ceiling(self):
        assert all(v <= BATCH_HARD_CEILING for v in BATCH_MAX_COMBINATIONS.values())


def test_router_registered():
    from src.api import batches_router

    assert any(getattr(rt, "path", None) == "/batches" for rt in batches_router.routes)


def test_dispatch_batch_run_registered_with_worker():
    """The worker only registers tasks in the Celery app `include`. If
    batch_tasks is missing, POST /batches enqueues an UNREGISTERED task that the
    worker drops -> the batch hangs forever at 'pending'. Caught in prod E2E;
    this guards the wiring."""
    from src.workers import app

    assert "src.workers.batch_tasks" in app.conf.include
    import src.workers.batch_tasks  # noqa: F401  (registers the @app.task)

    assert "src.workers.batch_tasks.dispatch_batch_run" in app.tasks


class TestDeliveryModeCreate:
    """create_batch must persist body.delivery_mode onto the ScraperBatch row
    (Codex P1) — without that explicit write, the DB's 'everything' default
    silently wins for every new batch, undoing the Pydantic default flip.

    Batch-create is Pro+ gated (BATCH_PLANS); no `pro_user` fixture exists in
    conftest, so `business_user`/`business_token` (also Pro+) stand in — same
    entitlement tier for this gate. king/WA/probate is a real, active
    county_connectors row seeded by migration 006, exercised elsewhere in this
    suite via the same county/record_type/state combination."""

    @staticmethod
    def _payload(**overrides: object) -> dict:
        payload: dict = {
            "state": "WA",
            "counties": ["king"],
            "record_types": ["probate"],
        }
        payload.update(overrides)
        return payload

    async def test_default_is_overlaps_only(
        self, client: AsyncClient, business_token: str, db: AsyncSession
    ):
        resp = await client.post(
            "/batches", json=self._payload(), headers=_auth(business_token)
        )
        assert resp.status_code == 201
        from sqlalchemy import select

        from src.db.models import ScraperBatch

        batch = (
            await db.execute(
                select(ScraperBatch).where(ScraperBatch.id == resp.json()["batch_id"])
            )
        ).scalar_one()
        assert batch.delivery_mode == "overlaps_only"

    async def test_explicit_mode_persisted(
        self, client: AsyncClient, business_token: str, db: AsyncSession
    ):
        resp = await client.post(
            "/batches",
            json=self._payload(delivery_mode="everything"),
            headers=_auth(business_token),
        )
        assert resp.status_code == 201
        from sqlalchemy import select

        from src.db.models import ScraperBatch

        batch = (
            await db.execute(
                select(ScraperBatch).where(ScraperBatch.id == resp.json()["batch_id"])
            )
        ).scalar_one()
        assert batch.delivery_mode == "everything"

    async def test_invalid_mode_422(self, client: AsyncClient, business_token: str):
        resp = await client.post(
            "/batches",
            json=self._payload(delivery_mode="bogus"),
            headers=_auth(business_token),
        )
        assert resp.status_code == 422
