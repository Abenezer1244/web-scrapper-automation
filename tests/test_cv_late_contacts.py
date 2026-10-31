"""Late delivery D1: a late row gets the paid skip trace its run had on, and nothing else does.

request_late_contacts calls the SAME enqueue a run uses, narrowed to the job's pending late
rows (only_result_ids) and quiet. Real Postgres; the Tracerfy token is a placeholder and
nothing is sent (the dispatcher is not run): these tests stop at what gets queued.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from src.config import settings
from src.db.models import Job, Result, ScraperConfig
from src.workers import cv_late_delivery as late
from tests.test_cv_late_delivery import _held, _held_row
from tests.test_lead_qualification import seattle


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(settings, "CV_LATE_DELIVERY_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")


async def _run(db, user, *, skip_on=True) -> str:
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="King code violations", county="king",
        state="WA", record_type="code_violation", fields=["party_name", "parcel_id"],
        enrichment=[], schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
        skip_trace_enabled=skip_on)
    db.add(cfg)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=cfg.id, status="done", trigger="manual",
               record_count=0, billed_count=0))
    await db.commit()
    return job_id


def _situs(**kw) -> dict:
    return {"property_city": "SEATTLE", "property_state": "WA", "property_zip": "98118", **kw}


async def _queued(db, job_id) -> set[str]:
    return set((await db.execute(text(
        "SELECT result_id::text FROM pending_skip_trace_rows WHERE job_id = :j"), {"j": job_id})).scalars())


async def _contacts(db, rid):
    return (await db.execute(text(
        "SELECT enrichment_data::jsonb->'late_delivery'->>'contacts' FROM results WHERE id = :i"),
        {"i": rid})).scalar()


@pytest.mark.asyncio
async def test_only_the_late_row_is_traced_never_the_runs_other_untraced_leads(db, business_user):
    job = await _run(db, business_user)
    held = await _held_row(db, business_user, job, _held(**_situs()))
    # A delivered lead of the same run the run did NOT trace (not_attempted): late delivery
    # must not pay for it.
    other = str(uuid.uuid4())
    db.add(Result(id=other, job_id=job, user_id=business_user.id, is_duplicate=False,
                  dedup_hash=uuid.uuid4().hex,
                  **{k: v for k, v in seattle(property_address="9 OTHER ST", **_situs()).items()
                     if k not in ("phone", "email")}))
    await db.commit()

    stats = late.deliver_completed_held_rows()

    assert stats["delivered"] == 1 and stats["contacts_requested"] == 1
    assert await _queued(db, job) == {held}
    assert await _contacts(db, held) == "requested"
    late.deliver_completed_held_rows()                       # never asked twice
    assert await _queued(db, job) == {held}


@pytest.mark.asyncio
async def test_a_scraper_without_skip_trace_gets_none(db, business_user):
    job = await _run(db, business_user, skip_on=False)
    held = await _held_row(db, business_user, job, _held(**_situs()))
    stats = late.deliver_completed_held_rows()
    assert stats["delivered"] == 1 and stats.get("contacts_requested", 0) == 0
    assert await _contacts(db, held) is None and await _queued(db, job) == set()


@pytest.mark.asyncio
async def test_a_standing_gate_is_recorded_and_a_waiting_gate_stays_pending(db, business_user, monkeypatch):
    job = await _run(db, business_user)
    held = await _held_row(db, business_user, job, _held(**_situs()))
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", False)          # waits: switched off globally
    late.deliver_completed_held_rows()
    assert await _contacts(db, held) == "pending" and await _queued(db, job) == set()

    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    await db.execute(text("UPDATE users SET plan = 'starter' WHERE id = :u"), {"u": business_user.id})
    await db.commit()
    late.deliver_completed_held_rows()                                   # stands: Starter has no skip trace
    assert await _contacts(db, held) == "not_requested" and await _queued(db, job) == set()


@pytest.mark.asyncio
async def test_a_done_jobs_log_is_not_appended_to(db, business_user):
    job = await _run(db, business_user)
    await _held_row(db, business_user, job, _held(**_situs()))
    before = (await db.execute(text("SELECT count(*) FROM job_logs WHERE job_id = :j"), {"j": job})).scalar()
    late.deliver_completed_held_rows()
    assert (await db.execute(text("SELECT count(*) FROM job_logs WHERE job_id = :j"), {"j": job})).scalar() == before


@pytest.mark.asyncio
async def test_asking_again_after_a_lost_marker_never_queues_or_pays_twice(db, business_user, monkeypatch):
    """Codex D1 r1 P1: the enqueue commits before the marker; if the marker is lost the row is
    asked again, and the enqueue must find nothing to buy (the row is no longer
    not_attempted, and the active-row index refuses a second pending row)."""
    job = await _run(db, business_user)
    held = await _held_row(db, business_user, job, _held(**_situs()))
    monkeypatch.setattr(late, "_MARK_CONTACTS_SQL", "UPDATE results SET no_such_column = 1")
    late.deliver_completed_held_rows()
    assert await _contacts(db, held) == "pending" and await _queued(db, job) == {held}
    monkeypatch.undo()
    monkeypatch.setattr(settings, "CV_LATE_DELIVERY_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")

    late.deliver_completed_held_rows()

    assert await _contacts(db, held) == "requested"
    assert (await db.execute(text("SELECT count(*) FROM pending_skip_trace_rows WHERE result_id = :r"),
                             {"r": held})).scalar() == 1
