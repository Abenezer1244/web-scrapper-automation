"""Durable claim handoff in the skip-trace dispatcher (Codex High, 2026-09-02).

DB-backed, no network: the Tracerfy endpoint is pointed at a non-HTTPS URL so
`submit_batch` raises a DEFINITE configuration rejection before any socket is
opened. That exercises the whole claim path — rows are claimed ('submitting',
committed) before the POST, the definite rejection releases them, the Result
rows are untouched, and rows another tick already claimed are never picked up.
Seeding uses system_sync_session (the worker write path), like test_analytics.
"""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from src.config import settings
from src.db.session import system_sync_session
from src.workers.skip_trace_dispatcher import dispatch_pending_skip_trace


def _seed_pending(user_id: str, *, status: str = "queued", submitted_at=None) -> tuple[str, str]:
    """scraper_config → job → result (skip_trace_status='queued') → pending row."""
    sc_id, job_id, result_id, pending_id = (str(uuid.uuid4()) for _ in range(4))
    with system_sync_session() as db:
        db.execute(
            text("""
                INSERT INTO scraper_configs
                    (id, user_id, name, county, state, record_type, fields, enrichment,
                     schedule, deliver, skip_trace_enabled, active)
                VALUES (:sc_id, :user_id, 'claim test', 'pierce', 'WA', 'probate',
                        '[]'::json, '[]'::json, '{"frequency":"manual"}'::json,
                        '{"format":"csv","emails":[]}'::json, true, true)
            """),
            {"sc_id": sc_id, "user_id": user_id},
        )
        db.execute(
            text("""
                INSERT INTO jobs (id, user_id, scraper_config_id, status, trigger,
                                  page_current, page_total, record_count, retry_count)
                VALUES (:job_id, :user_id, :sc_id, 'done', 'manual', 0, 0, 0, 0)
            """),
            {"job_id": job_id, "user_id": user_id, "sc_id": sc_id},
        )
        db.execute(
            text("""
                INSERT INTO results (id, job_id, user_id, is_duplicate, skip_trace_status,
                                     party_name, property_address, created_at)
                VALUES (:rid, :job_id, :user_id, false, 'queued',
                        'SAARENAS AVELINO G', '5128 BEVERLY AVE NE', now())
            """),
            {"rid": result_id, "job_id": job_id, "user_id": user_id},
        )
        db.execute(
            text("""
                INSERT INTO pending_skip_trace_rows
                    (id, job_id, result_id, user_id, property_address, city, state,
                     trace_type, status, enqueued_at, submitted_at)
                VALUES (:pid, :job_id, :rid, :user_id, '5128 BEVERLY AVE NE', 'TACOMA', 'WA',
                        'advanced', :status, now(), :submitted_at)
            """),
            {"pid": pending_id, "job_id": job_id, "rid": result_id, "user_id": user_id,
             "status": status, "submitted_at": submitted_at},
        )
        db.commit()
    return pending_id, result_id


def _pending_state(pending_id: str) -> tuple[str, object]:
    with system_sync_session() as db:
        return db.execute(
            text("SELECT status, submitted_at FROM pending_skip_trace_rows WHERE id = :id"),
            {"id": pending_id},
        ).one()


def _result_status(result_id: str) -> str:
    with system_sync_session() as db:
        return db.execute(
            text("SELECT skip_trace_status FROM results WHERE id = :id"), {"id": result_id}
        ).scalar_one()


@pytest.fixture
def _dispatcher_enabled(monkeypatch):
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")
    # Non-HTTPS → submit_batch raises "must use HTTPS" BEFORE opening a socket:
    # a definite provider_error, no network, no credits.
    monkeypatch.setattr(settings, "TRACERFY_API_BASE_URL", "http://tracerfy.invalid")
    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "")  # alerts stay no-op


@pytest.mark.asyncio
async def test_definite_rejection_releases_claim_to_errored(starter_user, _dispatcher_enabled):
    pending_id, result_id = _seed_pending(starter_user.id)

    out = dispatch_pending_skip_trace()

    assert out["submitted_batches"] == 0
    assert any("HTTPS" in e for e in out["errors"])
    status, submitted_at = _pending_state(pending_id)
    # Claimed ('submitting', committed) and then RELEASED by the definite failure —
    # never left mid-claim, never re-queued for a definite rejection.
    assert status == "errored"
    assert submitted_at is None
    # A definite rejection surfaces on the lead too — "Error", not "Processing"
    # forever (Codex round 3).
    assert _result_status(result_id) == "errored"


@pytest.mark.asyncio
async def test_row_claimed_by_another_tick_is_not_resubmitted(starter_user, _dispatcher_enabled):
    stale = datetime.now(UTC) - timedelta(hours=2)
    pending_id, result_id = _seed_pending(starter_user.id, status="submitting", submitted_at=stale)

    out = dispatch_pending_skip_trace()

    # Nothing to submit: 'submitting' rows belong to another tick / a crashed
    # handoff and must never be paid for again. The stale-claim check ran
    # (OPS_ALERT_EMAIL empty → no-op) without touching the row.
    assert out == {"submitted_batches": 0, "submitted_rows": 0, "errors": []}
    status, submitted_at = _pending_state(pending_id)
    assert status == "submitting"
    assert submitted_at is not None
    assert _result_status(result_id) == "queued"
