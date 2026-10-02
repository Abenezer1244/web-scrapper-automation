"""The PACS counties and Thurston wired into the shared mailing hook (county_gis).

Audit 2026-10-02: Benton, Chelan, Clallam, Grant, Island, Jefferson, Thurston and
Whatcom stored 0% mailing because county_gis knew no mailing source for them and fell
to the situs-only statewide layer. These tests pin the wiring end to end: which
counties have a source (and which wait on the license switch), what each adapter
outcome becomes in batch_enrich_parcels_gis, the live job pass, the completion line
for a county that still has no source, and the background recovery sweep.

Real DB rows and a real Redis lease. The adapters' own HTTP behaviour is pinned in
tests/test_pacs_parcel.py and tests/test_thurston_assessor.py; here the adapter's
answer is the boundary, so only `resolve_mailing` and the statewide situs layer are
substituted.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig
from src.scrapers.enrichment import county_gis as cg
from src.scrapers.enrichment import pacs_parcel as pp
from src.scrapers.enrichment import thurston_assessor as ta
from src.scrapers.enrichment.snohomish_assessor_roll import (
    AMBIGUOUS,
    FOUND,
    SOURCE_UNAVAILABLE,
    MailingAnswer,
)

_FOUND = "131073011125003"
_NONE = "131073011125004"
_AMBIG = "131073011125005"
_DOWN = "131073011125006"


@pytest.fixture(autouse=True)
def _situs_only_statewide(monkeypatch):
    """The statewide layer answers situs, never mailing: exactly what it does live."""
    monkeypatch.setattr(cg, "_batch_query_wa_statewide", lambda pids, county: {
        p: {"property_address": "65003 N SR 225", "mailing_address": None, "parcel_id": p,
            "property_city": "BENTON CITY", "property_state": "WA", "property_zip": "99320"}
        for p in pids})


def _answers(monkeypatch, answers: dict, *, module=pp) -> list:
    """Substitute the adapter's answer. Records each call's (county?, ids)."""
    calls: list = []
    if module is pp:
        def _resolve(county, ids, **kw):
            calls.append((county, list(ids)))
            return {p: answers.get(p, MailingAnswer(SOURCE_UNAVAILABLE)) for p in ids}
    else:
        def _resolve(ids, **kw):
            calls.append(("thurston", list(ids)))
            return {p: answers.get(p, MailingAnswer(SOURCE_UNAVAILABLE)) for p in ids}
    monkeypatch.setattr(module, "resolve_mailing", _resolve)
    return calls


_BENTON = {
    _FOUND: MailingAnswer(FOUND, mailing_address="PO BOX 800, PHOENIX, AZ 85001", role="owner"),
    _NONE: MailingAnswer("none"),
    _AMBIG: MailingAnswer(AMBIGUOUS),
}


# ─── Which counties have a source ────────────────────────────────────────────

class TestRegistry:
    @pytest.mark.parametrize("county", ["benton", "clallam", "jefferson", "thurston"])
    def test_unrestricted_counties_have_a_source_whatever_the_switch(self, county, monkeypatch):
        from src.config import settings

        for flag in (True, False):
            monkeypatch.setattr(settings, "COUNTY_GIS_RESTRICTED_MAILING_ENABLED", flag)
            assert cg.has_mailing_source(county, "WA") is True

    @pytest.mark.parametrize("county", ["grant", "whatcom", "island", "chelan"])
    def test_rcw_clause_counties_answer_to_the_license_switch(self, county, monkeypatch):
        from src.config import settings

        monkeypatch.setattr(settings, "COUNTY_GIS_RESTRICTED_MAILING_ENABLED", False)
        assert cg.has_mailing_source(county, "WA") is False
        monkeypatch.setattr(settings, "COUNTY_GIS_RESTRICTED_MAILING_ENABLED", True)
        assert cg.has_mailing_source(county, "WA") is True

    @pytest.mark.parametrize("county", ["kitsap", "okanogan", "whitman", "douglas"])
    def test_counties_without_a_parcel_source_stay_without_one(self, county, monkeypatch):
        from src.config import settings

        monkeypatch.setattr(settings, "COUNTY_GIS_RESTRICTED_MAILING_ENABLED", True)
        assert cg.has_mailing_source(county, "WA") is False

    def test_recovery_sees_every_wired_county(self, monkeypatch):
        from src.config import settings

        monkeypatch.setattr(settings, "COUNTY_GIS_RESTRICTED_MAILING_ENABLED", True)
        counties = set(cg.gis_mailing_source_counties("WA"))
        assert {"benton", "clallam", "jefferson", "grant", "whatcom", "island", "chelan",
                "thurston", "clark", "snohomish", "pierce", "cowlitz"} <= counties

    def test_every_pacs_county_in_the_hook_has_a_portal(self):
        hooked = {k[:-3] for k, v in cg._BULK_MAILING_SOURCES.items() if v.startswith("pacs_")}
        assert hooked == set(pp.PACS_SITES)


# ─── batch_enrich_parcels_gis: each adapter outcome ──────────────────────────

class TestBatchEnrich:
    def test_benton_outcomes(self, monkeypatch):
        calls = _answers(monkeypatch, _BENTON)
        stats: dict = {}
        out = cg.batch_enrich_parcels_gis([_FOUND, _NONE, _AMBIG, _DOWN], "Benton", "WA", stats=stats)
        assert calls == [("benton", [_FOUND, _NONE, _AMBIG, _DOWN])]
        # found: the county's address, its provenance, and the situs kept beside it
        assert out[_FOUND]["mailing_address"] == "PO BOX 800, PHOENIX, AZ 85001"
        assert out[_FOUND]["mailing_source"] == "pacs_benton"
        assert out[_FOUND]["property_address"] == "65003 N SR 225"
        # none: a settled answer, recorded, not deferred
        assert out[_NONE]["mailing_address"] is None
        assert out[_NONE]["mailing_lookup"] == "none"
        assert _NONE not in stats["county_unreached"]
        # ambiguous on a live page: deferred, but it spends an attempt ("error")
        assert out[_AMBIG]["mailing_lookup"] == "error"
        assert _AMBIG in stats["county_unreached"]
        # not reached: deferred for free
        assert _DOWN in stats["county_unreached"]
        assert "mailing_lookup" not in out[_DOWN]

    def test_the_property_address_is_never_copied_into_mailing(self, monkeypatch):
        _answers(monkeypatch, {_NONE: MailingAnswer("none")})
        out = cg.batch_enrich_parcels_gis([_NONE], "benton", "WA")
        assert out[_NONE]["property_address"] == "65003 N SR 225"
        assert out[_NONE]["mailing_address"] is None

    def test_thurston_dispatches_to_its_own_adapter(self, monkeypatch):
        calls = _answers(monkeypatch, {"74700001201": MailingAnswer(
            FOUND, mailing_address="3000 PACIFIC AVE SE, OLYMPIA, WA 98501", role="taxpayer")}, module=ta)
        out = cg.batch_enrich_parcels_gis(["74700001201"], "thurston", "WA")
        assert calls == [("thurston", ["74700001201"])]
        assert out["74700001201"]["mailing_source"] == "thurston_assessor"
        assert out["74700001201"]["mailing_role"] == "taxpayer"

    def test_a_restricted_county_makes_no_request_with_the_switch_off(self, monkeypatch):
        from src.config import settings

        monkeypatch.setattr(settings, "COUNTY_GIS_RESTRICTED_MAILING_ENABLED", False)
        calls = _answers(monkeypatch, _BENTON)
        stats: dict = {}
        out = cg.batch_enrich_parcels_gis([_FOUND], "whatcom", "WA", stats=stats)
        assert calls == []
        assert out[_FOUND]["mailing_address"] is None

    def test_an_adapter_that_raises_defers_everything_it_was_asked(self, monkeypatch):
        def _boom(county, ids, **kw):
            raise RuntimeError("portal changed")
        monkeypatch.setattr(pp, "resolve_mailing", _boom)
        stats: dict = {}
        out = cg.batch_enrich_parcels_gis([_FOUND, _NONE], "benton", "WA", stats=stats)
        assert {out[p]["mailing_address"] for p in (_FOUND, _NONE)} == {None}
        assert set(stats["county_unreached"]) >= {_FOUND, _NONE}


# ─── The live job pass and the recovery sweep, against the real DB ───────────

async def _job(db, user, county: str, *, status: str = "enriching") -> str:
    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name=f"{county} probate wiring",
        county=county, state="WA", record_type="probate",
        fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
        deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status=status,
               trigger="manual", record_count=1, billed_count=1))
    await db.commit()
    return job_id


async def _row(db, user, job_id: str, parcel: str, *, enrichment: dict | None = None) -> str:
    rid = str(uuid.uuid4())
    db.add(Result(
        id=rid, user_id=user.id, job_id=job_id, party_name="DOE JANE A",
        doc_type="DEATH CERTIFICATE", parcel_id=parcel, property_address=None,
        mailing_address=None, enrichment_data=enrichment or {"instrument_number": "1"},
        skip_trace_status="not_attempted",
    ))
    await db.commit()
    return rid


async def _get(db, rid: str):
    return (await db.execute(text(
        "SELECT mailing_address, property_address, enrichment_data, absentee_owner, "
        "out_of_state_owner, skip_trace_status FROM results WHERE id = :i"), {"i": rid})).first()


async def _run_job_enrichment(db, job_id, redis_client) -> dict:
    def _go():
        from src.db.session import system_sync_session
        from src.workers.tasks_helpers.enrich import _run_inline_enrichment

        with system_sync_session() as sdb:
            job = sdb.get(Job, job_id)
            config = sdb.get(ScraperConfig, job.scraper_config_id)
            summary: dict = {}
            _run_inline_enrichment(sdb, job, redis_client, job_id, config, summary=summary)
            return summary

    return await asyncio.to_thread(_go)


class TestJobPass:
    async def test_benton_job_fills_settles_and_defers(self, db, business_user, redis_client, monkeypatch):
        _answers(monkeypatch, _BENTON)
        job_id = await _job(db, business_user, "benton")
        found = await _row(db, business_user, job_id, _FOUND)
        none = await _row(db, business_user, job_id, _NONE)
        down = await _row(db, business_user, job_id, _DOWN)
        summary = await _run_job_enrichment(db, job_id, redis_client)

        f = await _get(db, found)
        assert f.mailing_address == "PO BOX 800, PHOENIX, AZ 85001"
        assert f.property_address == "65003 N SR 225"
        assert f.enrichment_data["mailing_source"] == "pacs_benton"
        # Owner flags are recomputed at the job's single post-enrichment choke point
        # (tasks.py, after refetch), not inside this pass; the recovery/backfill write
        # recomputes them itself (tests/test_backfill_mailing_by_county.py).
        n = await _get(db, none)
        assert n.mailing_address is None
        assert n.enrichment_data["mailing_recovery_outcome"] == "none"
        assert "mailing_lookup_deferred" not in n.enrichment_data
        d = await _get(db, down)
        assert d.mailing_address is None
        assert d.enrichment_data["mailing_lookup_deferred"] is True
        assert summary["mailing_deferred"] == 1
        # Mailing enrichment never depends on, or buys, a skip trace.
        assert {f.skip_trace_status, n.skip_trace_status, d.skip_trace_status} == {"not_attempted"}

    async def test_a_county_with_no_source_reports_missing_mailing_not_complete(
            self, db, business_user, redis_client, monkeypatch):
        """Kitsap has no mailing source yet. Its completion line must say so; it used
        to read "Enrichment complete: addresses added" over zero mailing addresses."""
        from src.workers.tasks_helpers.enrich import enrichment_completion_log

        job_id = await _job(db, business_user, "kitsap")
        await _row(db, business_user, job_id, "432000000801")
        await _row(db, business_user, job_id, "432000000802")
        summary = await _run_job_enrichment(db, job_id, redis_client)
        assert summary["mailing_missing"] == 2
        level, msg = enrichment_completion_log(summary)
        assert level == "info" and "2 leads have no mailing address available" in msg


class TestRecovery:
    async def test_a_deferred_benton_row_is_recovered_by_the_sweep(
            self, db, business_user, redis_client, monkeypatch):
        from src.workers import mailing_recovery as mr

        _answers(monkeypatch, _BENTON)
        job_id = await _job(db, business_user, "benton", status="done")
        rid = await _row(db, business_user, job_id, _FOUND,
                         enrichment={"mailing_lookup_deferred": True})
        await db.execute(text("UPDATE results SET property_address = '65003 N SR 225' WHERE id = :i"),
                         {"i": rid})
        await db.commit()
        stats = await asyncio.to_thread(mr.recover_deferred_gis_mailing)
        r = await _get(db, rid)
        assert r.mailing_address == "PO BOX 800, PHOENIX, AZ 85001"
        assert r.enrichment_data["mailing_recovery_outcome"] == "found"
        assert r.enrichment_data["mailing_lookup_deferred"] is False
        assert r.enrichment_data["mailing_source"] == "pacs_benton"
        assert stats["found"] >= 1

    async def test_an_ambiguous_page_spends_an_attempt_and_stays_deferred(
            self, db, business_user, redis_client, monkeypatch):
        from src.workers import mailing_recovery as mr

        _answers(monkeypatch, _BENTON)
        job_id = await _job(db, business_user, "benton", status="done")
        rid = await _row(db, business_user, job_id, _AMBIG,
                         enrichment={"mailing_lookup_deferred": True})
        await db.execute(text("UPDATE results SET property_address = '65003 N SR 225' WHERE id = :i"),
                         {"i": rid})
        await db.commit()
        await asyncio.to_thread(mr.recover_deferred_gis_mailing)
        r = await _get(db, rid)
        assert r.mailing_address is None
        assert r.enrichment_data["mailing_recovery_attempts"] == 1
        assert r.enrichment_data["mailing_lookup_deferred"] is True
