"""Mailing recovery for King leads whose recorder printed the 12-digit tax ACCOUNT number.

Such a lead was looked up as printed, which can never match a 10-digit PIN: its phase-1
page named a different parcel, no mailing request was ever charged, and the sweep rotated
it every tick forever (seen live 2026-09-14 on 012603938700 and 192106911402). Now the
sweep asks about the resolved PIN, and a page that names a different parcel settles.

Real DB, real rows; the county lookup boundary is substituted, as in test_mailing_recovery.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig
from src.workers import mailing_recovery as mr


@pytest.fixture(autouse=True)
def _healthy_no_lock(monkeypatch):
    monkeypatch.setattr("src.scrapers.enrichment.source_health.is_source_available", lambda *_a, **_k: True)
    monkeypatch.setattr(mr, "_acquire_single_flight", lambda: False)
    monkeypatch.setattr(mr, "_release_single_flight", lambda _c: None)


async def _row(db, user, parcel, *, extra=None) -> str:
    config = ScraperConfig(id=str(uuid.uuid4()), user_id=user.id, name="King prefc", county="king", state="WA",
                           record_type="pre_foreclosure", fields=["party_name"], enrichment=[],
                           schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []})
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status="done", trigger="manual",
               record_count=1, billed_count=1))
    await db.commit()
    rid = str(uuid.uuid4())
    db.add(Result(id=rid, user_id=user.id, job_id=job_id, party_name="RONSTAD ERIC R", parcel_id=parcel,
                  property_address="302 NW 203RD ST, SHORELINE, WA 98177", mailing_address=None,
                  skip_trace_status="not_attempted", is_duplicate=False,
                  enrichment_data={"mailing_lookup_deferred": True, **(extra or {})}))
    await db.commit()
    return rid


@pytest.mark.asyncio
async def test_a_resolved_account_number_is_asked_about_its_pin(db, business_user, monkeypatch):
    rid = await _row(db, business_user, "012603938700",
                     extra={"resolved_parcel_id": "0126039387", "resolved_by": "rpacct_account_number"})
    asked: list = []

    async def _page(parcels, **kw):
        asked.extend(parcels)
        kw["stats"].update({"requested_pids": list(parcels), "mailing_attempted_pids": list(parcels)})
        return {p: {"mailing_address": "302 NW 203RD ST, SHORELINE, WA 98177", "mailing_lookup": "found"}
                for p in parcels}

    monkeypatch.setattr("src.scrapers.enrichment.king_county_assessor.batch_enrich_king_county", _page)
    stats = await asyncio.to_thread(mr.recover_deferred_king_mailing)

    assert asked == ["0126039387"] and stats["found"] == 1
    row = (await db.execute(text("SELECT parcel_id, mailing_address, enrichment_data FROM results WHERE id = :i"),
                            {"i": rid})).first()
    assert row.parcel_id == "012603938700"
    assert row.mailing_address == "302 NW 203RD ST, SHORELINE, WA 98177"


@pytest.mark.asyncio
async def test_a_page_naming_a_different_parcel_settles_instead_of_rotating(db, business_user, monkeypatch):
    rid = await _row(db, business_user, "201260393870")
    asked: list = []

    async def _page(parcels, **kw):
        asked.append(list(parcels))
        kw["stats"].update({"requested_pids": list(parcels), "mailing_attempted_pids": []})
        return {p: {"property_address": None, "mailing_address": None, "parcel_lookup": "mismatch"}
                for p in parcels}

    monkeypatch.setattr("src.scrapers.enrichment.king_county_assessor.batch_enrich_king_county", _page)
    stats = await asyncio.to_thread(mr.recover_deferred_king_mailing)

    assert stats["parcel_mismatch"] == 1
    ed = (await db.execute(text("SELECT enrichment_data FROM results WHERE id = :i"), {"i": rid})).scalar()
    assert ed["mailing_recovery_outcome"] == "parcel_mismatch"
    assert ed["mailing_lookup_deferred"] is False and ed["mailing_recovery_attempts"] == 1

    await asyncio.to_thread(mr.recover_deferred_king_mailing)
    assert asked == [["201260393870"]]                      # never asked again


def test_the_property_sweep_is_registered_and_scheduled():
    from src.workers import app
    from src.workers.scheduler import app as _beat_app  # noqa: F401  (loads the schedule)

    assert "src.workers.property_recovery" in app.conf.include
    entries = {e["task"] for e in app.conf.beat_schedule.values()}
    assert "src.workers.property_recovery.recover_deferred_property" in entries
