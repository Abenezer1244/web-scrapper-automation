"""Late delivery D2: a late-delivered lead reaches the run's dialer once, never twice.

push_late_dialer decides each late row once, under the job then the row lock: left to the
sweep when the run was not pushed yet, skipped when the run was pushed too close to the
delivery to know (a duplicate import cannot be undone), not eligible without Business+ or a
destination, waits for its own skip trace (24 h at most), then the sweep's full predicate,
then claim-first and send. Real Postgres; the generic webhook's Celery enqueue is captured
at the boundary (nothing is sent anywhere).
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from src.config import settings
from src.db.models import Job, PendingSkipTraceRow, ScraperConfig
from src.workers import cv_late_delivery as late
from tests.test_cv_late_delivery import _held, _held_row

PHONE = "2065550100"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(settings, "CV_LATE_DELIVERY_ENABLED", True, raising=False)


async def _run(db, user, *, deliver=None, pushed_hours_ago: float | None = 2.0) -> str:
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="King code violations", county="king",
        state="WA", record_type="code_violation", fields=["party_name", "parcel_id"],
        enrichment=[], schedule={"frequency": "manual"},
        deliver=deliver if deliver is not None else {"format": "csv", "emails": [], "dialer_type": "phoneburner"})
    db.add(cfg)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=cfg.id, status="done", trigger="manual",
               record_count=0, billed_count=0))
    await db.commit()
    if pushed_hours_ago is not None:
        await db.execute(text("UPDATE jobs SET dialer_pushed_at = now() - make_interval(secs => :s) "
                              "WHERE id = :j"), {"s": pushed_hours_ago * 3600, "j": job_id})
        await db.commit()
    return job_id


async def _dialer(db, rid):
    return (await db.execute(text(
        "SELECT enrichment_data::jsonb->'late_delivery'->>'dialer' FROM results WHERE id = :i"),
        {"i": rid})).scalar()


async def _outbox(db, job) -> set[str]:
    return set((await db.execute(text("SELECT result_id::text FROM dialer_deliveries WHERE job_id = :j"),
                                 {"j": job})).scalars())


@pytest.mark.asyncio
async def test_a_late_lead_goes_to_the_runs_outbox_dialer_once(db, business_user):
    job = await _run(db, business_user)
    rid = await _held_row(db, business_user, job, _held(), phone=PHONE)

    stats = late.deliver_completed_held_rows()

    assert stats["delivered"] == 1 and stats["dialer_pushed"] == 1
    assert await _outbox(db, job) == {rid} and await _dialer(db, rid) == "pushed"
    late.deliver_completed_held_rows()
    assert await _outbox(db, job) == {rid}


@pytest.mark.asyncio
async def test_a_run_not_pushed_yet_leaves_it_to_the_sweep(db, business_user):
    job = await _run(db, business_user, pushed_hours_ago=None)
    rid = await _held_row(db, business_user, job, _held(), phone=PHONE)
    late.deliver_completed_held_rows()
    assert await _dialer(db, rid) == "via_sweep" and await _outbox(db, job) == set()


@pytest.mark.asyncio
async def test_a_run_pushed_right_before_the_delivery_is_never_pushed_again(db, business_user):
    """The sweep reads its rows right after it claims the run; this one may be in that push."""
    job = await _run(db, business_user, pushed_hours_ago=0.0)
    rid = await _held_row(db, business_user, job, _held(), phone=PHONE)
    late.deliver_completed_held_rows()
    assert await _dialer(db, rid) == "skipped_overlap" and await _outbox(db, job) == set()


@pytest.mark.asyncio
async def test_it_waits_for_its_own_skip_trace_but_not_forever(db, business_user):
    job = await _run(db, business_user)
    rid = await _held_row(db, business_user, job, _held(), phone=PHONE)
    db.add(PendingSkipTraceRow(id=str(uuid.uuid4()), job_id=job, result_id=rid, user_id=business_user.id,
                               property_address="4512 S HOLLY ST", status="queued"))
    await db.commit()

    late.deliver_completed_held_rows()
    assert await _dialer(db, rid) == "pending" and await _outbox(db, job) == set()

    # 25 h later the trace is still in flight: push with what the row has. (The run's own
    # push was 2 days before the delivery, so this is not the overlap case.)
    await db.execute(text("UPDATE jobs SET dialer_pushed_at = now() - interval '48 hours' WHERE id = :j"),
                     {"j": job})
    await db.execute(text(
        "UPDATE results SET enrichment_data = jsonb_set(enrichment_data::jsonb, '{late_delivery,delivered_at}', "
        "to_jsonb((now() - interval '25 hours')::text))::json WHERE id = :i"), {"i": rid})
    await db.commit()
    late.push_late_dialer()
    assert await _dialer(db, rid) == "pushed" and await _outbox(db, job) == {rid}


@pytest.mark.asyncio
async def test_no_dialable_phone_or_no_business_plan_is_recorded_not_pushed(db, business_user, starter_user):
    job = await _run(db, business_user)
    no_phone = await _held_row(db, business_user, job, _held())
    late.deliver_completed_held_rows()
    assert await _dialer(db, no_phone) == "not_ready"

    await db.execute(text("UPDATE users SET plan = 'pro', records_limit = 5000 WHERE id = :u"),
                     {"u": starter_user.id})
    await db.commit()
    pro_job = await _run(db, starter_user)
    pro_row = await _held_row(db, starter_user, pro_job, _held(), phone=PHONE)
    late.deliver_completed_held_rows()
    assert await _dialer(db, pro_row) == "not_eligible" and await _outbox(db, pro_job) == set()


@pytest.mark.asyncio
async def test_a_generic_webhook_gets_one_sealed_request_for_the_one_lead(db, business_user, monkeypatch):
    from src.workers import webhook_delivery

    sent = []
    monkeypatch.setattr(webhook_delivery.deliver_job_webhook, "delay",
                        lambda job_id, sealed: sent.append((job_id, sealed)))
    job = await _run(db, business_user, deliver={"format": "csv", "emails": [],
                                                 "dialer_webhook_url": "https://dialer.example/hook"})
    rid = await _held_row(db, business_user, job, _held(), phone=PHONE)

    late.deliver_completed_held_rows()

    assert await _dialer(db, rid) == "pushed"
    assert len(sent) == 1 and sent[0][0] == job
    assert PHONE not in json.dumps(sent[0][1])                     # sealed: no PII readable in the task args


@pytest.mark.asyncio
async def test_a_send_that_fails_goes_back_to_pending(db, business_user, monkeypatch):
    from src.workers.scheduler_helpers import dialer as dialer_helpers

    def boom(*a, **k):
        raise RuntimeError("broker down")

    monkeypatch.setattr(dialer_helpers, "_materialize_dialer_outbox", boom)
    job = await _run(db, business_user)
    rid = await _held_row(db, business_user, job, _held(), phone=PHONE)
    late.deliver_completed_held_rows()
    assert await _dialer(db, rid) == "pending" and await _outbox(db, job) == set()
