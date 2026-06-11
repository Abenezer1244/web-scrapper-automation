"""Piece 2 Phase 2A.2 — batch create request validation + caps + router wiring.

Pure tests (no DB). The fan-out / gating SQL paths are exercised in CI.
"""
import pytest
from pydantic import ValidationError

from src.api.schemas import BatchCreateRequest
from src.config.constants import (
    BATCH_HARD_CEILING,
    BATCH_MAX_COMBINATIONS,
    BATCH_PLANS,
    Plan,
)


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
