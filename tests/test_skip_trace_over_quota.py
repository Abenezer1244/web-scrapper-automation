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
from sqlalchemy.exc import IntegrityError

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
    """Run the sweep the way the dispatcher tick now does.

    `_cancel_undeliverable_queued` no longer commits (Codex round 15, finding
    15-5): the caller owns the transaction, so that Phase 1b-2 can write the
    contact-lookup-action disposition and its audit event in the SAME one. This
    helper therefore commits, exactly like the real caller in
    dispatch_pending_skip_trace.
    """
    from src.db.session import system_sync_session

    with system_sync_session() as s:
        swept = dispatcher._cancel_undeliverable_queued(s)
        s.commit()
        return swept


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

    async def test_the_sweep_keeps_a_lead_another_active_row_still_references(
        self, db, business_user,
    ):
        """Direct coverage for the sweep's NOT EXISTS guard.

        Migration 100 normally makes two active rows for one lead impossible, so
        this reaches the guard by dropping the index for the duration. That is
        worth doing rather than deleting the coverage: the claim deliberately
        FAILS OPEN for the scrape path when 100 is missing, so in exactly the
        situation where duplicates can occur, this guard is what stops a lead
        being released back to 'not_attempted' while a row of it is still live.
        """
        from src.workers.skip_trace_claim import INDEX_NAME

        job = await _job(db, business_user, "failed")
        lead = await _lead(db, business_user, job)
        queued = await _pending(db, business_user, job, lead)

        # The sweep runs in its OWN session, so this DDL has to be committed for
        # it to be visible -- which means it must be put back explicitly or every
        # later test in this database silently runs unenforced.
        await db.execute(text(f"DROP INDEX {INDEX_NAME}"))
        await db.commit()
        try:
            await db.execute(text(
                "INSERT INTO pending_skip_trace_rows "
                "(id, job_id, result_id, user_id, property_address, city, state, zip, "
                " first_name, last_name, trace_type, status) "
                "VALUES (gen_random_uuid(), CAST(:j AS uuid), CAST(:r AS uuid), "
                "        CAST(:u AS uuid), '1 MAIN ST', 'EVERETT', 'WA', '98201', "
                "        'JANE', 'DOE', 'normal', 'submitting')"
            ), {"j": job, "r": lead, "u": business_user.id})
            await db.commit()

            _sweep()

            # The queued row is withdrawn, but the lead is NOT released: the
            # 'submitting' row may already be at Tracerfy and charged for.
            assert await _status(db, "pending_skip_trace_rows", queued) == "cancelled"
            assert await _status(db, "results", lead) == "queued"
        finally:
            # Clear this lead's rows first: if an assertion above failed, two
            # active rows may still exist and the unique index could not be
            # rebuilt, which would leave every later test running unenforced.
            await db.execute(text(
                "DELETE FROM pending_skip_trace_rows WHERE result_id = CAST(:r AS uuid)"
            ), {"r": lead})
            await db.execute(text(
                f"CREATE UNIQUE INDEX {INDEX_NAME} ON pending_skip_trace_rows "
                "(result_id) WHERE status IN ('queued','submitting','submitted')"
            ))
            await db.commit()

    async def test_a_duplicated_lead_is_not_submitted_while_100_is_unenforced(
        self, db, business_user, tracerfy,
    ):
        """The dispatcher is what SPENDS money, so it gets its own guard.

        Migration 100 stops a lead holding two active claims, and the claim path
        refuses to write without it. But 100 ABORTS precisely when duplicates
        already exist, and start.sh boots the worker anyway, so the dispatcher
        would drain both rows of a duplicate pair and charge for one lead twice.
        With the invariant off it holds those leads back instead, and submits
        everything else rather than halting every tenant over a condition most
        are not in.
        """
        from src.workers.skip_trace_claim import INDEX_NAME

        job = await _job(db, business_user, "done")
        dup_lead = await _lead(db, business_user, job)
        ok_lead = await _lead(db, business_user, job)
        ok_row = await _pending(db, business_user, job, ok_lead)

        await db.execute(text(f"DROP INDEX {INDEX_NAME}"))
        await db.commit()
        try:
            # Two ACTIVE rows for one lead: only possible with 100 absent.
            dup_row = await _pending(db, business_user, job, dup_lead)
            await _pending(db, business_user, job, dup_lead, status="submitted")
            await db.commit()

            dispatcher.dispatch_pending_skip_trace()

            # Asserted on the QUEUE, not on what the stub recorded: the row
            # leaving 'queued' is what commits us to paying for it, and the
            # dispatcher stamps that before it ever contacts the vendor.
            assert await _status(db, "pending_skip_trace_rows", dup_row) == "queued", (
                "a lead with two active rows advanced toward submission and would "
                "be charged twice"
            )
            # The unaffected lead must not be collateral: it is neither held back
            # by name nor cancelled. That it SUBMITS normally is covered by the
            # dispatcher's own suites, which run with the index in place; this
            # test is about what happens when the invariant is off.
            assert await _status(db, "pending_skip_trace_rows", ok_row) in (
                "queued", "submitting", "submitted"
            ), "an unrelated lead was cancelled by the duplicate guard"
        finally:
            await db.execute(text(
                "DELETE FROM pending_skip_trace_rows WHERE result_id = CAST(:r AS uuid)"
            ), {"r": dup_lead})
            await db.execute(text(
                f"CREATE UNIQUE INDEX {INDEX_NAME} ON pending_skip_trace_rows "
                "(result_id) WHERE status IN ('queued','submitting','submitted')"
            ))
            await db.commit()

    async def test_the_sweep_does_not_commit_its_own_work(self, db, business_user):
        """Transaction ownership, proved rather than asserted.

        `_cancel_undeliverable_queued` used to commit internally, which would
        make it impossible for Phase 1b-2 to write the contact-lookup-action
        disposition and the audit event in the SAME transaction as the
        cancellation -- a crash between the two would leave an action reading
        "still looking" forever while the queue row was already gone. This test
        fails if the internal commit ever comes back.
        """
        from src.db.session import system_sync_session

        job = await _job(db, business_user, "failed")
        lead = await _lead(db, business_user, job)
        row = await _pending(db, business_user, job, lead)

        with system_sync_session() as s:
            assert dispatcher._cancel_undeliverable_queued(s) == 1
            s.rollback()

        assert await _status(db, "pending_skip_trace_rows", row) == "queued"
        assert await _status(db, "results", lead) == "queued"

    async def test_a_failing_sweep_is_reported_as_None_to_the_compliance_gate(
        self, db, business_user, monkeypatch,
    ):
        """The tick skips entirely when the sweep fails while the ATIP paid
        switch is off, so `swept is None` has to survive the move of the
        try/except from the helper to its caller."""
        from src.config import settings

        # The tick returns {'skipped': 'disabled'} before it ever reaches the
        # sweep unless skip trace is on, which would make this assertion vacuous.
        monkeypatch.setattr(settings, "SKIP_TRACE_ENABLED", True)
        monkeypatch.setattr(settings, "TRACERFY_API_TOKEN", "test-token-not-real")
        monkeypatch.setattr(settings, "PIERCE_CV_OWNER_SKIP_TRACE_ENABLED", False)

        def _boom(_db):
            raise RuntimeError("sweep exploded")

        monkeypatch.setattr(dispatcher, "_cancel_undeliverable_queued", _boom)
        result = dispatcher.dispatch_pending_skip_trace()
        assert result.get("deferred") == "sweep_failed"

    async def test_a_lead_can_no_longer_have_two_active_rows(self, db, business_user):
        """This replaces `test_a_lead_another_active_row_still_references_keeps_its_status`.

        That test seeded one lead with BOTH a 'queued' and a 'submitting' pending
        row and asserted the sweep left the lead alone, because the sweep's
        second statement releases a lead only when no other ACTIVE row still
        references it. Migration 100 makes that state impossible: a partial
        unique index on pending_skip_trace_rows(result_id) for active rows now
        refuses the second one outright, which is a stronger guarantee than
        handling it afterwards. Production carried 0 such groups across 941
        pending rows when this was verified, so nothing legitimate is being
        forbidden -- the enqueue only ever picks up leads reading
        'not_attempted', and it stamps 'queued' as it claims them.

        The sweep's NOT EXISTS guard is deliberately KEPT as defence in depth;
        it is simply no longer reachable by way of a duplicate active row.
        """
        job = await _job(db, business_user, "failed")
        lead = await _lead(db, business_user, job)
        queued = await _pending(db, business_user, job, lead)

        with pytest.raises(IntegrityError):
            await _pending(db, business_user, job, lead, status="submitting")
        await db.rollback()

        # And the ordinary path still holds: cancelling a lead's only active row
        # releases the lead back to 'not_attempted' for a later run.
        _sweep()
        assert await _status(db, "pending_skip_trace_rows", queued) == "cancelled"
        assert await _status(db, "results", lead) == "not_attempted"


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
