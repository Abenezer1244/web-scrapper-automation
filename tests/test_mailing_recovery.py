"""Background recovery for King mailing lookups a source outage deferred.

`mailing_lookup_deferred` was written by the enrichment pass and read by nothing,
so "deferred" meant permanently skipped. This is the sweep that was promised in a
comment and never built, and these tests pin the boundaries that make it safe to
run unattended against already-delivered, already-billed rows.

The guarantees under test, in order of how expensive getting them wrong would be:

  * it never bills, never reserves quota and never creates a job;
  * it never enqueues a Tracerfy skip trace and never touches skip-trace state;
  * it never overwrites a mailing address another path already found;
  * it never writes a mailing address onto a row whose parcel changed underneath
    it;
  * it stops retrying, so a parcel that can never be answered cannot starve the
    rest of the backlog.

Real DB, real rows. The county lookup itself is substituted, because the point of
the test is what we WRITE, and asking King County to fail on cue is not available.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig, User
from src.workers import mailing_recovery as mr

pytestmark = pytest.mark.asyncio


async def _king_job(db, user: User) -> tuple[ScraperConfig, str]:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="King Recovery Config",
        county="king", state="WA", record_type="pre_foreclosure",
        fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id,
               status="done", trigger="manual", record_count=1, billed_count=1))
    await db.commit()
    return config, job_id


async def _deferred_row(db, user: User, job_id: str, *, parcel: str,
                        mailing: str | None = None, attempts: int | None = None) -> str:
    enrichment: dict = {"mailing_lookup_deferred": True}
    if attempts is not None:
        enrichment["mailing_recovery_attempts"] = attempts
    rid = str(uuid.uuid4())
    db.add(Result(
        id=rid, user_id=user.id, job_id=job_id, party_name="DOE JANE",
        parcel_id=parcel, property_address="1 MAIN ST, SEATTLE, WA 98101",
        mailing_address=mailing, enrichment_data=enrichment,
        skip_trace_status="not_attempted", is_duplicate=False,
    ))
    await db.commit()
    return rid


@pytest.fixture
def found_mailing(monkeypatch):
    """The county answers with a mailing address for every parcel asked."""
    async def _fake(parcels, **kw):
        st = kw.get("stats")
        if st is not None:
            st["deferred"] = []
        return {p: {"mailing_address": f"PO BOX {p[-4:]}, RENO, NV 89501",
                    "mailing_lookup": "found"} for p in parcels}

    monkeypatch.setattr(
        "src.scrapers.enrichment.king_county_assessor.batch_enrich_king_county", _fake
    )


@pytest.fixture
def no_mailing(monkeypatch):
    """The county answers, and this parcel genuinely has no mailing address."""
    async def _fake(parcels, **kw):
        st = kw.get("stats")
        if st is not None:
            st["deferred"] = []
        return {p: {"mailing_address": None, "mailing_lookup": "none"} for p in parcels}

    monkeypatch.setattr(
        "src.scrapers.enrichment.king_county_assessor.batch_enrich_king_county", _fake
    )


@pytest.fixture
def lookup_errors(monkeypatch):
    """The lookup runs but resolves nothing (transient)."""
    async def _fake(parcels, **kw):
        st = kw.get("stats")
        if st is not None:
            st["deferred"] = []
        return {p: {"mailing_address": None, "mailing_lookup": "error"} for p in parcels}

    monkeypatch.setattr(
        "src.scrapers.enrichment.king_county_assessor.batch_enrich_king_county", _fake
    )


@pytest.fixture(autouse=True)
def _source_is_healthy(monkeypatch):
    monkeypatch.setattr(
        "src.scrapers.enrichment.source_health.is_source_available", lambda *_a, **_k: True
    )


@pytest.fixture(autouse=True)
def _no_lock(monkeypatch):
    monkeypatch.setattr(mr, "_acquire_single_flight", lambda: False)
    monkeypatch.setattr(mr, "_release_single_flight", lambda _c: None)


class TestFillsWhatTheOutageMissed:
    async def test_a_deferred_row_gets_its_mailing_address(self, db, business_user,
                                                           found_mailing):
        _, job_id = await _king_job(db, business_user)
        rid = await _deferred_row(db, business_user, job_id, parcel="1234500001")

        stats = await asyncio.to_thread(mr.recover_deferred_king_mailing)
        assert stats["found"] == 1

        row = (await db.execute(
            text("SELECT mailing_address, enrichment_data FROM results WHERE id = :i"),
            {"i": rid})).first()
        assert row.mailing_address.startswith("PO BOX")
        # The marker clears, so the row stops being re-selected forever.
        assert row.enrichment_data["mailing_lookup_deferred"] is False
        assert row.enrichment_data["mailing_recovery_outcome"] == "found"

    async def test_one_lookup_serves_every_row_on_that_parcel(self, db, business_user,
                                                              found_mailing):
        # Two leads can share a parcel. Paying the county twice for one answer is
        # waste, and this is a background sweep with a whole backlog to get through.
        _, job_id = await _king_job(db, business_user)
        a = await _deferred_row(db, business_user, job_id, parcel="1234500002")
        b = await _deferred_row(db, business_user, job_id, parcel="1234500002")

        stats = await asyncio.to_thread(mr.recover_deferred_king_mailing)
        assert stats["parcels"] == 1
        for rid in (a, b):
            got = (await db.execute(
                text("SELECT mailing_address FROM results WHERE id = :i"), {"i": rid}
            )).scalar()
            assert got is not None


class TestNeverDamagesExistingData:
    async def test_an_existing_mailing_address_is_never_overwritten(self, db, business_user,
                                                                    found_mailing):
        _, job_id = await _king_job(db, business_user)
        rid = await _deferred_row(db, business_user, job_id, parcel="1234500003",
                                  mailing="999 REAL ST, SEATTLE, WA 98101")

        await asyncio.to_thread(mr.recover_deferred_king_mailing)
        got = (await db.execute(
            text("SELECT mailing_address FROM results WHERE id = :i"), {"i": rid})).scalar()
        assert got == "999 REAL ST, SEATTLE, WA 98101"

    async def test_unrelated_enrichment_metadata_survives(self, db, business_user,
                                                          found_mailing):
        _, job_id = await _king_job(db, business_user)
        rid = str(uuid.uuid4())
        db.add(Result(
            id=rid, user_id=business_user.id, job_id=job_id, party_name="DOE JANE",
            parcel_id="1234500004", property_address="1 MAIN ST",
            enrichment_data={"mailing_lookup_deferred": True, "situs_city": "SEATTLE",
                             "parcel_lookup": "verified"},
            skip_trace_status="not_attempted", is_duplicate=False,
        ))
        await db.commit()

        await asyncio.to_thread(mr.recover_deferred_king_mailing)
        ed = (await db.execute(
            text("SELECT enrichment_data FROM results WHERE id = :i"), {"i": rid})).scalar()
        # A whole-column replace would silently drop situs parts and parcel
        # provenance, which other features read.
        assert ed["situs_city"] == "SEATTLE"
        assert ed["parcel_lookup"] == "verified"

    async def test_a_row_whose_parcel_changed_is_skipped(self, db, business_user,
                                                         monkeypatch):
        """A repair may re-point a row while our lookup is in flight.

        Committing on the stale read would write the OLD parcel's mailing address
        onto the NEW parcel: a stranger's address on a real lead.
        """
        _, job_id = await _king_job(db, business_user)
        rid = await _deferred_row(db, business_user, job_id, parcel="1234500005")

        async def _fake(parcels, **kw):
            st = kw.get("stats")
            if st is not None:
                st["deferred"] = []
            # The repair lands between the SELECT and the UPDATE.
            from src.db.session import SyncSessionLocal
            with SyncSessionLocal() as s:
                s.execute(text("UPDATE results SET parcel_id = :p WHERE id = :i"),
                          {"p": "9999900000", "i": rid})
                s.commit()
            return {p: {"mailing_address": "PO BOX 1, RENO, NV",
                        "mailing_lookup": "found"} for p in parcels}

        monkeypatch.setattr(
            "src.scrapers.enrichment.king_county_assessor.batch_enrich_king_county", _fake
        )
        await asyncio.to_thread(mr.recover_deferred_king_mailing)
        got = (await db.execute(
            text("SELECT mailing_address FROM results WHERE id = :i"), {"i": rid})).scalar()
        assert got is None


class TestBillingAndSkipTraceAreUntouched:
    async def test_quota_and_billing_are_not_touched(self, db, business_user,
                                                     found_mailing):
        config, job_id = await _king_job(db, business_user)
        await _deferred_row(db, business_user, job_id, parcel="1234500006")
        before_used = (await db.execute(
            text("SELECT records_used FROM users WHERE id = :i"),
            {"i": business_user.id})).scalar()
        before_job = (await db.execute(
            text("SELECT record_count, billed_count, billing_applied_at, reserved_count "
                 "FROM jobs WHERE id = :i"), {"i": job_id})).first()

        await asyncio.to_thread(mr.recover_deferred_king_mailing)

        after_used = (await db.execute(
            text("SELECT records_used FROM users WHERE id = :i"),
            {"i": business_user.id})).scalar()
        after_job = (await db.execute(
            text("SELECT record_count, billed_count, billing_applied_at, reserved_count "
                 "FROM jobs WHERE id = :i"), {"i": job_id})).first()
        # Recovery adds data to an already-paid-for lead. It is not a new lead.
        assert after_used == before_used
        assert tuple(after_job) == tuple(before_job)

    async def test_no_skip_trace_is_enqueued_and_status_is_untouched(self, db, business_user,
                                                                     found_mailing):
        _, job_id = await _king_job(db, business_user)
        rid = await _deferred_row(db, business_user, job_id, parcel="1234500007")

        await asyncio.to_thread(mr.recover_deferred_king_mailing)

        pending = (await db.execute(
            text("SELECT count(*) FROM pending_skip_trace_rows WHERE job_id = :j"),
            {"j": job_id})).scalar()
        row = (await db.execute(
            text("SELECT skip_trace_status, phone, email FROM results WHERE id = :i"),
            {"i": rid})).first()
        # Filling a mailing address must never buy a Tracerfy lookup: skip trace
        # bills per call and keys off property_address, which this never changes.
        assert pending == 0
        assert row.skip_trace_status == "not_attempted"
        assert row.phone is None and row.email is None

    async def test_no_new_job_is_created(self, db, business_user, found_mailing):
        _, job_id = await _king_job(db, business_user)
        await _deferred_row(db, business_user, job_id, parcel="1234500008")
        before = (await db.execute(
            text("SELECT count(*) FROM jobs WHERE user_id = :u"),
            {"u": business_user.id})).scalar()

        await asyncio.to_thread(mr.recover_deferred_king_mailing)

        after = (await db.execute(
            text("SELECT count(*) FROM jobs WHERE user_id = :u"),
            {"u": business_user.id})).scalar()
        assert after == before


class TestTerminalPolicy:
    async def test_a_verified_absent_mailing_address_stops_retrying(self, db, business_user,
                                                                     no_mailing):
        _, job_id = await _king_job(db, business_user)
        rid = await _deferred_row(db, business_user, job_id, parcel="1234500009")

        stats = await asyncio.to_thread(mr.recover_deferred_king_mailing)
        assert stats["none"] == 1
        row = (await db.execute(
            text("SELECT mailing_address, enrichment_data FROM results WHERE id = :i"),
            {"i": rid})).first()
        # The source ANSWERED: there is no mailing address. Do not invent one, and
        # do not keep asking.
        assert row.mailing_address is None
        assert row.enrichment_data["mailing_lookup_deferred"] is False
        assert row.enrichment_data["mailing_recovery_outcome"] == "none"

    async def test_a_transient_error_stays_eligible(self, db, business_user, lookup_errors):
        _, job_id = await _king_job(db, business_user)
        rid = await _deferred_row(db, business_user, job_id, parcel="1234500010")

        await asyncio.to_thread(mr.recover_deferred_king_mailing)
        ed = (await db.execute(
            text("SELECT enrichment_data FROM results WHERE id = :i"), {"i": rid})).scalar()
        assert ed["mailing_lookup_deferred"] is True
        assert ed["mailing_recovery_attempts"] == 1

    async def test_retries_stop_at_the_ceiling(self, db, business_user, lookup_errors):
        _, job_id = await _king_job(db, business_user)
        rid = await _deferred_row(db, business_user, job_id, parcel="1234500011",
                                  attempts=mr._MAX_ATTEMPTS - 1)

        await asyncio.to_thread(mr.recover_deferred_king_mailing)
        ed = (await db.execute(
            text("SELECT enrichment_data FROM results WHERE id = :i"), {"i": rid})).scalar()
        # A parcel that can never be answered must stop consuming ticks, or it
        # starves every row behind it.
        assert ed["mailing_recovery_attempts"] == mr._MAX_ATTEMPTS
        assert ed["mailing_lookup_deferred"] is False

    async def test_a_row_past_the_ceiling_is_not_selected(self, db, business_user,
                                                          lookup_errors):
        _, job_id = await _king_job(db, business_user)
        await _deferred_row(db, business_user, job_id, parcel="1234500012",
                            attempts=mr._MAX_ATTEMPTS)
        stats = await asyncio.to_thread(mr.recover_deferred_king_mailing)
        assert stats["candidates"] == 0


class TestGating:
    async def test_nothing_runs_while_the_source_is_in_cooldown(self, db, business_user,
                                                                found_mailing, monkeypatch):
        monkeypatch.setattr(
            "src.scrapers.enrichment.source_health.is_source_available",
            lambda *_a, **_k: False,
        )
        _, job_id = await _king_job(db, business_user)
        rid = await _deferred_row(db, business_user, job_id, parcel="1234500013")

        stats = await asyncio.to_thread(mr.recover_deferred_king_mailing)
        assert "cooldown" in stats["skipped"]
        got = (await db.execute(
            text("SELECT mailing_address FROM results WHERE id = :i"), {"i": rid})).scalar()
        assert got is None

    async def test_rows_on_a_live_job_are_not_touched(self, db, business_user,
                                                      found_mailing):
        """Adding an address to a live job's row can change is_actionable under a
        job that is mid-count, mid-bill or mid-export."""
        config, job_id = await _king_job(db, business_user)
        await db.execute(text("UPDATE jobs SET status = 'enriching' WHERE id = :i"),
                         {"i": job_id})
        await db.commit()
        await _deferred_row(db, business_user, job_id, parcel="1234500014")

        stats = await asyncio.to_thread(mr.recover_deferred_king_mailing)
        assert stats["candidates"] == 0
