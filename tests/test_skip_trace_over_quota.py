"""Tracerfy must never be paid for a lead that will not be delivered.

Skip trace used to be queued inside enrichment, before the plan cap marked which
rows are over quota, and the dispatcher sent every queued row without checking
the lead or its job. So leads past a user's quota, and leads of a job that failed
before delivery, were traced and paid for.

Real DB and real Redis. The only substitute is Tracerfy's batch POST: the point
is which rows we would PAY for, and the provider cannot be asked to bill on cue.
"""
from __future__ import annotations

import os
import random
import uuid

import pytest
import redis as sync_redis
from sqlalchemy import text

from src.api.lead_actionability import DELIVERY_EXCLUDED_KEY, OVER_QUOTA
from src.db.models import Job, PendingSkipTraceRow, Result, ScraperConfig, User
from src.workers import skip_trace_dispatcher as dispatcher

pytestmark = pytest.mark.asyncio


async def _job(db, user: User, status: str) -> str:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="Skip Trace Quota Config",
        county="snohomish", state="WA", record_type="tax_delinquent",
        fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []}, skip_trace_enabled=True,
    )
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id,
               status=status, trigger="manual"))
    await db.commit()
    return job_id


async def _lead(db, user: User, job_id: str, *, status: str = "queued",
                over_quota: bool = False, duplicate: bool = False) -> str:
    rid = str(uuid.uuid4())
    db.add(Result(
        id=rid, user_id=user.id, job_id=job_id, party_name="DOE JANE",
        parcel_id=f"{random.randint(10**13, 10**14 - 1)}",
        property_address=f"{random.randint(100, 99999)} MAIN ST, EVERETT, WA 98201",
        enrichment_data={DELIVERY_EXCLUDED_KEY: OVER_QUOTA} if over_quota else {},
        skip_trace_status=status, is_duplicate=duplicate,
        # A re-election demoting the survivor writes 'same_run'. An already-delivered
        # ('prior_run') lead stays traceable: test_skip_trace_already_delivered.py.
        duplicate_reason="same_run" if duplicate else None,
    ))
    await db.commit()
    return rid


async def _pending(db, user: User, job_id: str, result_id: str, status: str = "queued") -> str:
    pid = str(uuid.uuid4())
    db.add(PendingSkipTraceRow(
        id=pid, job_id=job_id, result_id=result_id, user_id=user.id,
        property_address="1 MAIN ST", city="EVERETT", state="WA", zip="98201",
        first_name="JANE", last_name="DOE", trace_type="normal", status=status,
    ))
    await db.commit()
    return pid


async def _status(db, table: str, row_id: str) -> str:
    col = "status" if table == "pending_skip_trace_rows" else "skip_trace_status"
    return (await db.execute(
        text(f"SELECT {col} FROM {table} WHERE id = :i"), {"i": row_id}  # noqa: S608 - fixed names
    )).scalar_one()


def _sweep() -> int:
    from src.db.session import system_sync_session

    with system_sync_session() as s:
        return dispatcher._cancel_undeliverable_queued(s)


class TestCancelSweep:
    async def test_rows_of_a_failed_job_are_cancelled_and_the_lead_is_released(self, db, business_user):
        job = await _job(db, business_user, "failed")
        lead = await _lead(db, business_user, job)
        row = await _pending(db, business_user, job, lead)
        _sweep()
        assert await _status(db, "pending_skip_trace_rows", row) == "cancelled"
        assert await _status(db, "results", lead) == "not_attempted"

    async def test_over_quota_and_duplicate_leads_of_a_done_job_are_cancelled(self, db, business_user):
        job = await _job(db, business_user, "done")
        capped = await _pending(db, business_user, job, await _lead(db, business_user, job, over_quota=True))
        dup = await _pending(db, business_user, job, await _lead(db, business_user, job, duplicate=True))
        _sweep()
        assert await _status(db, "pending_skip_trace_rows", capped) == "cancelled"
        assert await _status(db, "pending_skip_trace_rows", dup) == "cancelled"

    async def test_a_deliverable_lead_and_a_running_job_are_left_alone(self, db, business_user):
        done = await _job(db, business_user, "done")
        running = await _job(db, business_user, "enriching")
        ok = await _pending(db, business_user, done, await _lead(db, business_user, done))
        waiting = await _pending(db, business_user, running, await _lead(db, business_user, running))
        _sweep()
        assert await _status(db, "pending_skip_trace_rows", ok) == "queued"
        assert await _status(db, "pending_skip_trace_rows", waiting) == "queued"

    async def test_an_atip_named_tacoma_lead_is_withdrawn_while_the_paid_switch_is_off(
        self, db, business_user, monkeypatch,
    ):
        """Legal cleared NAMING a Tacoma owner from Pierce ATIP, not buying data keyed on
        that name. A row enqueued before PIERCE_CV_OWNER_SKIP_TRACE_ENABLED was turned off
        is withdrawn here, before the submit loop."""
        from src.config import settings

        job = await _job(db, business_user, "done")
        atip = await _lead(db, business_user, job)
        other = await _lead(db, business_user, job)
        await db.execute(text(
            "UPDATE results SET enrichment_data = CAST(:ed AS json) WHERE id = :i"),
            {"ed": '{"source": "tacoma_code_violations", "owner_source": "pierce_atip"}', "i": atip})
        await db.commit()
        atip_row = await _pending(db, business_user, job, atip)
        other_row = await _pending(db, business_user, job, other)

        _sweep()

        assert await _status(db, "pending_skip_trace_rows", atip_row) == "cancelled"
        assert await _status(db, "results", atip) == "not_attempted"
        assert await _status(db, "pending_skip_trace_rows", other_row) == "queued"

        # With the switch on, the same row is left queued for Tracerfy.
        monkeypatch.setattr(settings, "PIERCE_CV_OWNER_SKIP_TRACE_ENABLED", True)
        await db.execute(text(
            "UPDATE pending_skip_trace_rows SET status = 'queued' WHERE id = :i"), {"i": atip_row})
        await db.execute(text(
            "UPDATE results SET skip_trace_status = 'queued' WHERE id = :i"), {"i": atip})
        await db.commit()

        _sweep()

        assert await _status(db, "pending_skip_trace_rows", atip_row) == "queued"

    async def test_a_row_already_at_tracerfy_is_never_touched(self, db, business_user):
        job = await _job(db, business_user, "failed")
        lead = await _lead(db, business_user, job, status="submitted")
        row = await _pending(db, business_user, job, lead, status="submitting")
        _sweep()
        assert await _status(db, "pending_skip_trace_rows", row) == "submitting"
        assert await _status(db, "results", lead) == "submitted"

    async def test_a_lead_another_active_row_still_references_keeps_its_status(self, db, business_user):
        job = await _job(db, business_user, "failed")
        lead = await _lead(db, business_user, job)
        queued = await _pending(db, business_user, job, lead)
        await _pending(db, business_user, job, lead, status="submitting")
        _sweep()
        assert await _status(db, "pending_skip_trace_rows", queued) == "cancelled"
        assert await _status(db, "results", lead) == "queued"


@pytest.fixture
def tracerfy(monkeypatch):
    """Records what WOULD be sent to Tracerfy; nothing leaves the process."""
    from src.config import settings
    from src.scrapers.enrichment import skip_trace

    monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
    monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")
    monkeypatch.setattr(settings, "SKIP_TRACE_MAX_BATCHES_PER_TICK", 1)
    sent: list[str] = []

    def _submit(rows, trace_type="normal", api_token=None):
        sent.extend(str(r.get("result_id") or r.get("id")) for r in rows)
        return {"queue_id": random.randint(10**8, 2 * 10**9), "rows_uploaded": len(rows),
                "credits_deducted": 0}

    monkeypatch.setattr(skip_trace, "submit_batch", _submit)
    return sent


class TestDispatcherPaysOnlyForDeliverableLeads:
    async def test_only_the_deliverable_lead_of_a_done_job_is_submitted(self, db, business_user, tracerfy):
        done = await _job(db, business_user, "done")
        running = await _job(db, business_user, "enriching")
        failed = await _job(db, business_user, "failed")
        ok_lead = await _lead(db, business_user, done)
        ok = await _pending(db, business_user, done, ok_lead)
        capped = await _pending(db, business_user, done, await _lead(db, business_user, done, over_quota=True))
        waiting = await _pending(db, business_user, running, await _lead(db, business_user, running))
        dead = await _pending(db, business_user, failed, await _lead(db, business_user, failed))

        dispatcher.dispatch_pending_skip_trace()

        assert await _status(db, "pending_skip_trace_rows", ok) == "submitted"
        assert await _status(db, "results", ok_lead) == "submitted"
        assert await _status(db, "pending_skip_trace_rows", capped) == "cancelled"
        assert await _status(db, "pending_skip_trace_rows", dead) == "cancelled"
        # A running job's row waits for DONE instead of going out mid-job.
        assert await _status(db, "pending_skip_trace_rows", waiting) == "queued"


async def _atip_lead(db, user: User, job_id: str) -> str:
    rid = await _lead(db, user, job_id)
    await db.execute(text("UPDATE results SET enrichment_data = CAST(:ed AS json) WHERE id = :i"),
                     {"ed": '{"source": "tacoma_code_violations", "owner_source": "pierce_atip"}',
                      "i": rid})
    await db.commit()
    return rid


class TestAtipPaidUseFailsClosed:
    """Legal cleared NAMING a Tacoma owner from Pierce ATIP, not buying contact data keyed
    on that name. Neither half of the dispatcher may submit such a row while
    PIERCE_CV_OWNER_SKIP_TRACE_ENABLED is off — including when the cancel sweep fails."""

    async def test_the_submit_query_skips_an_atip_row_the_sweep_did_not_cancel(
        self, db, business_user, tracerfy, monkeypatch,
    ):
        monkeypatch.setattr(dispatcher, "_cancel_undeliverable_queued", lambda _db: 0)
        done = await _job(db, business_user, "done")
        atip = await _pending(db, business_user, done, await _atip_lead(db, business_user, done))
        ok = await _pending(db, business_user, done, await _lead(db, business_user, done))

        dispatcher.dispatch_pending_skip_trace()

        assert await _status(db, "pending_skip_trace_rows", atip) == "queued"
        assert await _status(db, "pending_skip_trace_rows", ok) == "submitted"
        assert len(tracerfy) == 1

    async def test_a_failed_sweep_stops_the_tick_instead_of_submitting(
        self, db, business_user, tracerfy, monkeypatch,
    ):
        monkeypatch.setattr(dispatcher, "_cancel_undeliverable_queued", lambda _db: None)
        done = await _job(db, business_user, "done")
        ok = await _pending(db, business_user, done, await _lead(db, business_user, done))

        out = dispatcher.dispatch_pending_skip_trace()

        assert out.get("deferred") == "sweep_failed" and tracerfy == []
        assert await _status(db, "pending_skip_trace_rows", ok) == "queued"

    async def test_with_the_paid_switch_on_a_failed_sweep_does_not_stop_the_tick(
        self, db, business_user, tracerfy, monkeypatch,
    ):
        from src.config import settings

        monkeypatch.setattr(settings, "PIERCE_CV_OWNER_SKIP_TRACE_ENABLED", True)
        monkeypatch.setattr(dispatcher, "_cancel_undeliverable_queued", lambda _db: None)
        done = await _job(db, business_user, "done")
        atip = await _pending(db, business_user, done, await _atip_lead(db, business_user, done))

        dispatcher.dispatch_pending_skip_trace()

        assert await _status(db, "pending_skip_trace_rows", atip) == "submitted"


class TestEnqueueHappensAfterTheCap:
    async def test_the_enqueue_skips_leads_the_cap_marked_over_quota(self, db, business_user, monkeypatch):
        from src.config import settings
        from src.db.session import SyncSessionLocal
        from src.workers.tasks_helpers.enrich import _enqueue_skip_trace_rows

        monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
        monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")
        job_id = await _job(db, business_user, "enriching")
        delivered = await _lead(db, business_user, job_id, status="not_attempted")
        capped = await _lead(db, business_user, job_id, status="not_attempted", over_quota=True)

        r = sync_redis.Redis.from_url(os.environ["REDIS_URL"])
        with SyncSessionLocal() as s:
            job = s.get(Job, job_id)
            config = s.get(ScraperConfig, job.scraper_config_id)
            _enqueue_skip_trace_rows(s, job, r, job_id, config)

        queued_for = {str(x) for x in (await db.execute(
            text("SELECT result_id FROM pending_skip_trace_rows WHERE job_id = :j"), {"j": job_id}
        )).scalars()}
        assert delivered in queued_for
        assert capped not in queued_for
        assert await _status(db, "results", capped) == "not_attempted"

    async def test_enrichment_no_longer_enqueues_skip_trace(self):
        import inspect

        from src.workers.tasks_helpers import enrich

        body = inspect.getsource(enrich._run_inline_enrichment)
        assert "_enqueue_skip_trace_rows(" not in body
