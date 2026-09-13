"""County-GIS mailing addresses: a failed lookup is deferred, and the deferral recovers.

Before this, a county whose own GIS layer publishes the owner's mailing address
(Snohomish, Cowlitz, Pierce) had one shot per job. If that request failed, the row kept
a NULL mailing address that looked exactly like "the county has none", and nothing
ever asked again. These tests pin both halves:

  * the job's enrichment pass marks an UNREACHED parcel `mailing_lookup_deferred`,
    and does not mark one the county answered;
  * the background sweep fills it later without billing, skip tracing, overwriting,
    or touching a row on a live job, and recomputes the owner-location flags that
    depend on the address it wrote.

Real DB, real rows. Only the county HTTP boundary is substituted.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig, User
from src.scrapers.enrichment import county_gis as cg
from src.workers import mailing_recovery as mr

pytestmark = pytest.mark.asyncio

TX_MAIL = "PO BOX 961089, FORT WORTH, TX 76161-0089"


async def _job(db, user: User, *, county: str, status: str = "done") -> str:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name=f"{county} GIS mailing",
        county=county, state="WA", record_type="pre_foreclosure",
        fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id,
               status=status, trigger="manual", record_count=1, billed_count=1))
    await db.commit()
    return job_id


async def _row(db, user: User, job_id: str, *, parcel: str, deferred: bool = True,
               mailing: str | None = None, attempts: int | None = None) -> str:
    enrichment: dict = {"situs_note": "kept"}
    if deferred:
        enrichment["mailing_lookup_deferred"] = True
    if attempts is not None:
        enrichment["mailing_recovery_attempts"] = attempts
    rid = str(uuid.uuid4())
    db.add(Result(
        id=rid, user_id=user.id, job_id=job_id, party_name="JASO ANTHONY R",
        parcel_id=parcel, property_address="22801 64TH PL W",
        property_city="MOUNTLAKE TERRACE", property_state="WA", property_zip="98043",
        mailing_address=mailing, enrichment_data=enrichment,
        skip_trace_status="not_attempted", is_duplicate=False,
    ))
    await db.commit()
    return rid


async def _get(db, rid: str):
    return (await db.execute(text(
        "SELECT mailing_address, enrichment_data, owner_state, absentee_owner, "
        "out_of_state_owner, property_address, skip_trace_status, phone "
        "FROM results WHERE id = :i"), {"i": rid})).first()


def _county_answers(monkeypatch, answers: dict, unreached: tuple[str, ...] = ()):
    """Stand in for the county layer. `answers` maps parcel -> mailing (or None)."""
    calls: list[tuple[list[str], str]] = []

    def _fake(parcel_ids, county, state, stats=None):
        calls.append((list(parcel_ids), county))
        if stats is not None:
            stats["county_unreached"] = [p for p in parcel_ids if p in unreached]
        return {p: {"property_address": "22801 64TH PL W", "mailing_address": answers[p]}
                for p in parcel_ids if p in answers and p not in unreached}

    monkeypatch.setattr(cg, "batch_enrich_parcels_gis", _fake)
    return calls


class TestRecoverySweep:
    async def test_a_deferred_row_gets_the_county_mailing_and_fresh_owner_flags(
        self, db, business_user, monkeypatch,
    ):
        job_id = await _job(db, business_user, county="snohomish")
        rid = await _row(db, business_user, job_id, parcel="00522400008900")
        _county_answers(monkeypatch, {"00522400008900": TX_MAIL})

        stats = await asyncio.to_thread(mr.recover_deferred_gis_mailing)

        assert stats["found"] == 1
        row = await _get(db, rid)
        assert row.mailing_address == TX_MAIL
        assert row.enrichment_data["mailing_lookup_deferred"] is False
        assert row.enrichment_data["mailing_recovery_outcome"] == "found"
        assert row.enrichment_data["situs_note"] == "kept"
        # Texas mail for a Washington property: the flags the filters read follow it.
        assert row.owner_state == "TX"
        assert row.out_of_state_owner is True
        assert row.absentee_owner is True

    async def test_the_county_answering_with_no_mailing_is_terminal(
        self, db, business_user, monkeypatch,
    ):
        job_id = await _job(db, business_user, county="cowlitz")
        rid = await _row(db, business_user, job_id, parcel="08931001")
        _county_answers(monkeypatch, {"08931001": None})

        await asyncio.to_thread(mr.recover_deferred_gis_mailing)

        row = await _get(db, rid)
        assert row.mailing_address is None
        assert row.enrichment_data["mailing_recovery_outcome"] == "none"
        assert row.enrichment_data["mailing_lookup_deferred"] is False
        # NULL stays NULL and nothing is inferred from the property address.
        assert row.owner_state is None

    async def test_an_unreached_parcel_is_not_charged_an_attempt(
        self, db, business_user, monkeypatch,
    ):
        job_id = await _job(db, business_user, county="snohomish")
        rid = await _row(db, business_user, job_id, parcel="00647500007600", attempts=2)
        _county_answers(monkeypatch, {}, unreached=("00647500007600",))

        await asyncio.to_thread(mr.recover_deferred_gis_mailing)

        row = await _get(db, rid)
        assert row.mailing_address is None
        assert row.enrichment_data["mailing_lookup_deferred"] is True
        assert row.enrichment_data["mailing_recovery_attempts"] == 2
        assert row.enrichment_data.get("mailing_recovery_last_at")

    async def test_an_existing_mailing_address_is_never_selected_or_overwritten(
        self, db, business_user, monkeypatch,
    ):
        job_id = await _job(db, business_user, county="snohomish")
        rid = await _row(db, business_user, job_id, parcel="30072900302800",
                         mailing="5919 218TH AVE NE, REDMOND, WA 98053")
        calls = _county_answers(monkeypatch, {"30072900302800": TX_MAIL})

        await asyncio.to_thread(mr.recover_deferred_gis_mailing)

        assert calls == []
        assert (await _get(db, rid)).mailing_address == "5919 218TH AVE NE, REDMOND, WA 98053"

    async def test_counties_without_a_gis_mailing_source_are_left_alone(
        self, db, business_user, monkeypatch,
    ):
        # King's mailing comes from eRealProperty (its own sweep); Clark has no source.
        king = await _job(db, business_user, county="king")
        clark = await _job(db, business_user, county="clark")
        await _row(db, business_user, king, parcel="1234500001")
        await _row(db, business_user, clark, parcel="986012345")
        calls = _county_answers(monkeypatch, {"1234500001": TX_MAIL, "986012345": TX_MAIL})

        await asyncio.to_thread(mr.recover_deferred_gis_mailing)

        assert calls == []

    async def test_rows_on_a_live_job_are_not_touched(self, db, business_user, monkeypatch):
        job_id = await _job(db, business_user, county="snohomish", status="enriching")
        rid = await _row(db, business_user, job_id, parcel="00522400008900")
        _county_answers(monkeypatch, {"00522400008900": TX_MAIL})

        await asyncio.to_thread(mr.recover_deferred_gis_mailing)

        assert (await _get(db, rid)).mailing_address is None

    async def test_billing_quota_and_skip_trace_are_untouched(
        self, db, business_user, monkeypatch,
    ):
        job_id = await _job(db, business_user, county="cowlitz")
        rid = await _row(db, business_user, job_id, parcel="2231502")
        _county_answers(monkeypatch, {"2231502": TX_MAIL})
        used_before = (await db.execute(text("SELECT records_used FROM users WHERE id = :i"),
                                        {"i": business_user.id})).scalar()
        job_before = (await db.execute(text(
            "SELECT record_count, billed_count, billing_applied_at, reserved_count "
            "FROM jobs WHERE id = :i"), {"i": job_id})).first()
        jobs_before = (await db.execute(text("SELECT count(*) FROM jobs"))).scalar()

        await asyncio.to_thread(mr.recover_deferred_gis_mailing)

        assert (await db.execute(text("SELECT records_used FROM users WHERE id = :i"),
                                 {"i": business_user.id})).scalar() == used_before
        assert tuple((await db.execute(text(
            "SELECT record_count, billed_count, billing_applied_at, reserved_count "
            "FROM jobs WHERE id = :i"), {"i": job_id})).first()) == tuple(job_before)
        assert (await db.execute(text("SELECT count(*) FROM jobs"))).scalar() == jobs_before
        assert (await db.execute(text(
            "SELECT count(*) FROM pending_skip_trace_rows WHERE job_id = :j"),
            {"j": job_id})).scalar() == 0
        row = await _get(db, rid)
        assert (row.skip_trace_status, row.phone) == ("not_attempted", None)
        assert row.property_address == "22801 64TH PL W"

    async def test_one_lookup_serves_every_row_on_a_parcel(self, db, business_user,
                                                           monkeypatch):
        job_id = await _job(db, business_user, county="snohomish")
        a = await _row(db, business_user, job_id, parcel="00522400008900")
        b = await _row(db, business_user, job_id, parcel="00522400008900")
        calls = _county_answers(monkeypatch, {"00522400008900": TX_MAIL})

        stats = await asyncio.to_thread(mr.recover_deferred_gis_mailing)

        assert [c[0] for c in calls] == [["00522400008900"]]
        assert stats["parcels"] == 1
        for rid in (a, b):
            assert (await _get(db, rid)).mailing_address == TX_MAIL


class TestCountyUnreachedIsReported:
    """The batch must say which parcels it never got an answer for."""

    class _Resp:
        def __init__(self, code, payload):
            self.status_code = code
            self._payload = payload

        def json(self):
            return self._payload

    def _run(self, monkeypatch, behaviour):
        monkeypatch.setattr(cg, "safe_get", behaviour)
        monkeypatch.setattr(cg, "_batch_query_wa_statewide", lambda *a, **kw: {})
        stats: dict = {}
        out = cg.batch_enrich_parcels_gis(["00522400008900"], "snohomish", "WA", stats=stats)
        return out, stats["county_unreached"]

    def test_http_error(self, monkeypatch):
        _, unreached = self._run(monkeypatch, lambda *a, **kw: self._Resp(503, {}))
        assert unreached == ["00522400008900"]

    def test_arcgis_error_body(self, monkeypatch):
        _, unreached = self._run(monkeypatch, lambda *a, **kw: self._Resp(
            200, {"error": {"code": 499, "message": "Token Required"}}))
        assert unreached == ["00522400008900"]

    def test_exception(self, monkeypatch):
        def _boom(*a, **kw):
            raise TimeoutError("read timed out")
        _, unreached = self._run(monkeypatch, _boom)
        assert unreached == ["00522400008900"]

    def test_an_answer_with_no_feature_is_not_unreached(self, monkeypatch):
        _, unreached = self._run(monkeypatch, lambda *a, **kw: self._Resp(200, {"features": []}))
        assert unreached == []

    def test_mailing_source_registry(self):
        assert cg.gis_mailing_source_counties("WA") == ["cowlitz", "pierce", "snohomish"]
        assert cg.has_gis_mailing_source("king", "WA") is False
        assert cg.has_gis_mailing_source("clark", "WA") is False


class TestJobEnrichmentDefersUnreachedParcels:
    """The live job pass, run for real against the DB with only HTTP substituted."""

    async def _enrich(self, db, business_user, redis_client, monkeypatch, *, county,
                      parcel, safe_get):
        job_id = await _job(db, business_user, county=county, status="enriching")
        rid = await _row(db, business_user, job_id, parcel=parcel, deferred=False)
        monkeypatch.setattr(cg, "safe_get", safe_get)

        def _go():
            from src.db.session import system_sync_session
            from src.workers.tasks_helpers.enrich import _run_inline_enrichment

            with system_sync_session() as sdb:
                job = sdb.get(Job, job_id)
                config = sdb.get(ScraperConfig, job.scraper_config_id)
                summary: dict = {}
                _run_inline_enrichment(sdb, job, redis_client, job_id, config, summary=summary)
                return summary

        summary = await asyncio.to_thread(_go)
        return rid, summary

    async def test_a_failed_county_request_marks_the_row_deferred(
        self, db, business_user, redis_client, monkeypatch,
    ):
        def _down(*a, **kw):
            raise TimeoutError("county GIS timed out")

        rid, summary = await self._enrich(db, business_user, redis_client, monkeypatch,
                                          county="snohomish", parcel="00522400008900",
                                          safe_get=_down)
        row = await _get(db, rid)
        assert row.mailing_address is None
        assert row.enrichment_data["mailing_lookup_deferred"] is True
        assert row.enrichment_data["situs_note"] == "kept"
        assert summary.get("mailing_deferred") == 1  # one parcel

    async def test_a_county_that_answered_is_not_deferred(
        self, db, business_user, redis_client, monkeypatch,
    ):
        class _Resp:
            status_code = 200

            def json(self):
                return {"features": [{"attributes": {
                    "parcel_id": "00522400008900", "situsline1": "22801 64TH PL W",
                    "situscity": "MOUNTLAKE TERRACE", "situsstate": "WA", "situszip": "98043",
                    "taxprline1": "PO BOX 961089", "taxprcity": "FORT WORTH",
                    "taxprstate": "TX", "taxprzip": "76161-0089"}}]}

        rid, summary = await self._enrich(db, business_user, redis_client, monkeypatch,
                                          county="snohomish", parcel="00522400008900",
                                          safe_get=lambda *a, **kw: _Resp())
        row = await _get(db, rid)
        assert row.mailing_address == TX_MAIL
        assert "mailing_lookup_deferred" not in row.enrichment_data
        assert not summary.get("mailing_deferred")

    async def test_a_county_with_no_mailing_source_is_never_marked(
        self, db, business_user, redis_client, monkeypatch,
    ):
        def _down(*a, **kw):
            raise TimeoutError("statewide timed out")

        rid, summary = await self._enrich(db, business_user, redis_client, monkeypatch,
                                          county="clark", parcel="986012345",
                                          safe_get=_down)
        row = await _get(db, rid)
        assert "mailing_lookup_deferred" not in row.enrichment_data
        assert not summary.get("mailing_deferred")


# ─── Codex gate, round 3 (2026-09-13) ─────────────────────────────────────────

def test_parcels_left_off_a_truncated_page_are_unreached(monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {"exceededTransferLimit": True, "features": [{"attributes": {
                "parcel_id": "00522400008900", "situsline1": "22801 64TH PL W",
                "taxprline1": "PO BOX 1", "taxprcity": "LYNNWOOD", "taxprstate": "WA",
                "taxprzip": "98046"}}]}

    monkeypatch.setattr(cg, "safe_get", lambda *a, **kw: _Resp())
    monkeypatch.setattr(cg, "_batch_query_wa_statewide", lambda *a, **kw: {})
    stats: dict = {}
    out = cg.batch_enrich_parcels_gis(["00522400008900", "00647500007600"], "snohomish",
                                      "WA", stats=stats)
    assert out["00522400008900"]["mailing_address"] == "PO BOX 1, LYNNWOOD, WA 98046"
    assert stats["county_unreached"] == ["00647500007600"]


def test_a_street_only_cowlitz_match_keeps_the_layer_state():
    parsed = cg._parse_gis_response({"features": [{"attributes": {
        "PARCNO": "08931001", "SITUS_STREET_NUMBER": "3738",
        "SITUS_STREET_NAME": "PENNSYLVANIA", "SITUS_STREET_SUFFIX": "ST",
    }}]}, cg._KNOWN_GIS_ENDPOINTS["cowlitz_WA"])
    assert parsed["property_address"] == "3738 PENNSYLVANIA ST"
    assert parsed["property_state"] == "WA"
    assert parsed["property_city"] is None


def test_a_cowlitz_match_with_nothing_located_asserts_no_state():
    parsed = cg._parse_gis_response({"features": [{"attributes": {"PARCNO": "08931001"}}]},
                                    cg._KNOWN_GIS_ENDPOINTS["cowlitz_WA"])
    assert "property_state" not in parsed


async def test_the_gis_sweep_does_not_run_when_another_tick_holds_the_lock(
    db, business_user, monkeypatch,
):
    job_id = await _job(db, business_user, county="snohomish")
    await _row(db, business_user, job_id, parcel="00522400008900")
    calls = _county_answers(monkeypatch, {"00522400008900": TX_MAIL})
    monkeypatch.setattr(mr, "_acquire_single_flight", lambda: None)

    tick = await asyncio.to_thread(mr.run_mailing_recovery_tick)

    assert calls == []
    assert tick["king"]["skipped"] == "another tick is running"


# ─── Codex gate, round 4 (2026-09-13) ─────────────────────────────────────────

def test_a_po_box_after_a_numbered_department_is_where_the_street_starts():
    cfg = {"mailing_street_fields": ["taxprline1"],
           "mailing_locality_fields": ["taxprcity", "taxprstate", "taxprzip"]}
    out = cg._compose_mailing({"taxprline1": "DEPT 42 PO BOX 330310", "taxprcity": "SEATTLE",
                               "taxprstate": "WA", "taxprzip": "98133"}, cfg)
    assert out == "PO BOX 330310, SEATTLE, WA 98133"


async def test_the_king_sweep_gets_only_what_the_gis_sweep_left(monkeypatch):
    import time as _time

    monkeypatch.setattr(mr, "_acquire_single_flight", lambda: False)
    monkeypatch.setattr(mr, "_release_single_flight", lambda _c: None)
    clock = {"now": 1000.0}
    monkeypatch.setattr(_time, "monotonic", lambda: clock["now"])

    def _slow_gis():
        clock["now"] += mr._TICK_BUDGET_S - 30   # GIS ate almost the whole tick
        return {"parcels": 0}

    monkeypatch.setattr(mr, "recover_deferred_gis_mailing", _slow_gis)
    tick = await asyncio.to_thread(mr.run_mailing_recovery_tick)
    assert tick["king"]["skipped"] == "tick budget spent before the King sweep"


class TestCommitFailureKeepsTheDeferral(TestJobEnrichmentDefersUnreachedParcels):
    async def test_markers_survive_a_rolled_back_gis_batch(
        self, db, business_user, redis_client, monkeypatch,
    ):
        """The batch commit fails once. Its fills are lost, but the rows must still be
        queued for recovery, and the summary must count only what was stored."""
        from sqlalchemy.orm import Session

        real_commit = Session.commit
        state = {"failed": False}

        def _flaky_commit(self_):
            if not state["failed"] and any(
                isinstance(o, Result) and o.mailing_address for o in self_.dirty
            ):
                state["failed"] = True
                raise RuntimeError("simulated commit failure")
            return real_commit(self_)

        monkeypatch.setattr(Session, "commit", _flaky_commit)

        class _Resp:
            status_code = 200

            def json(self):
                return {"features": [{"attributes": {
                    "parcel_id": "00522400008900", "situsline1": "22801 64TH PL W",
                    "taxprline1": "PO BOX 961089", "taxprcity": "FORT WORTH",
                    "taxprstate": "TX", "taxprzip": "76161-0089"}}]}

        rid, summary = await self._enrich(db, business_user, redis_client, monkeypatch,
                                          county="snohomish", parcel="00522400008900",
                                          safe_get=lambda *a, **kw: _Resp())
        assert state["failed"] is True
        row = await _get(db, rid)
        assert row.mailing_address is None
        assert row.enrichment_data["mailing_lookup_deferred"] is True
        assert summary.get("mailing_deferred") == 1  # one parcel


# ─── Codex gate, round 5 (2026-09-13) ─────────────────────────────────────────

async def test_the_gis_kill_switch_stops_the_sweep(db, business_user, monkeypatch):
    from src.config import settings

    job_id = await _job(db, business_user, county="snohomish")
    rid = await _row(db, business_user, job_id, parcel="00522400008900")
    calls = _county_answers(monkeypatch, {"00522400008900": TX_MAIL})
    monkeypatch.setattr(settings, "GIS_ENRICHMENT_ENABLED", False, raising=False)

    stats = await asyncio.to_thread(mr.recover_deferred_gis_mailing)

    assert calls == [] and stats["skipped"] == "GIS_ENRICHMENT_ENABLED is off"
    assert (await _get(db, rid)).enrichment_data["mailing_lookup_deferred"] is True
