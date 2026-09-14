"""Background recovery of King property addresses a job could not look up.

King GIS has no condo UNIT features, so a unit's property address depends on the condo
extract or the per-parcel eRealProperty page. When the page did not run (lease busy,
breaker, budget) the lead kept a blank property address forever. A job now marks such
leads `property_lookup_deferred = true`; this sweep is the reading half.

Contract pinned here: only marked, delivered leads on done King jobs; the extract before
any page request; the page asked with the resolved PIN, property only; a value only onto a
row still empty and unchanged; settled outcomes stop retries, transient ones are charged
and capped, unreached ones rotate uncharged; kill switch and cooldown stop page requests;
no billing, quota, skip trace or mailing change.

Real DB, real Redis lock, real source-health gate. The county extract is a real zip in
tmp_path; King GIS and the page lookup boundary are substituted.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from src.db.models import Job, Result, ScraperConfig, User
from src.scrapers.enrichment import king_condo_units as kc
from src.scrapers.enrichment.source_health import KING_EREALPROPERTY
from src.workers import property_recovery as prc
from tests.test_king_condo_unit_situs import G204, _condo_zip, _gis_row, _unit

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from src.config import settings
    from src.db.session import SyncSessionLocal

    monkeypatch.setattr(settings, "PROPERTY_RECOVERY_ENABLED", True, raising=False)

    def _wipe():
        with SyncSessionLocal() as sdb:
            sdb.execute(text("DELETE FROM external_source_health WHERE source_key = :k"),
                        {"k": KING_EREALPROPERTY})
            sdb.commit()

    _wipe()
    yield
    _wipe()


async def _job(db, user: User, *, county="king", status="done", record_type="pre_foreclosure") -> str:
    config = ScraperConfig(id=str(uuid.uuid4()), user_id=user.id, name="property recovery", county=county,
                           state="WA", record_type=record_type, fields=["party_name"], enrichment=[],
                           schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []})
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status=status, trigger="manual",
               record_count=1, billed_count=1))
    await db.commit()
    return job_id


async def _lead(db, user, job_id, parcel, *, marked=True, mailing="806 LAKESHORE DR, REDWOOD, CA 94065",
                extra=None, duplicate=False, prop=None) -> str:
    rid = str(uuid.uuid4())
    ed = {"source": "king_landmark_json", **({"property_lookup_deferred": True} if marked else {}), **(extra or {})}
    db.add(Result(id=rid, user_id=user.id, job_id=job_id, party_name="DOE JANE", parcel_id=parcel,
                  property_address=prop, mailing_address=mailing, skip_trace_status="not_attempted",
                  is_duplicate=duplicate, dedup_hash=f"h-{rid}", enrichment_data=ed))
    await db.commit()
    return rid


async def _get(db, rid):
    return (await db.execute(text(
        "SELECT property_address, property_city, property_zip, mailing_address, absentee_owner, "
        "enrichment_data, skip_trace_status, parcel_id FROM results WHERE id = :i"), {"i": rid})).first()


def _gis(monkeypatch, table):
    def _g(parcel_ids, county, state, stats=None):
        return {p: dict(table[p]) for p in parcel_ids if p in table}
    monkeypatch.setattr("src.scrapers.enrichment.county_gis.batch_enrich_parcels_gis", _g)


def _page(monkeypatch, answer):
    asked: list = []

    async def _erp(parcel_ids, **kw):
        asked.append((list(parcel_ids), kw.get("do_mailing")))
        kw["stats"]["requested_pids"] = list(parcel_ids)
        return {p: dict(answer(p)) for p in parcel_ids if answer(p) is not None}

    monkeypatch.setattr("src.scrapers.enrichment.king_county_assessor.batch_enrich_king_county", _erp)
    return asked


def _tick():
    return prc.recover_deferred_king_property()


async def test_the_extract_fills_a_unit_without_asking_the_page(db, business_user, tmp_path, monkeypatch):
    job_id = await _job(db, business_user)
    rid = await _lead(db, business_user, job_id, "0268000490")
    monkeypatch.setattr(kc, "cached_extract", lambda *a, **kw: (_condo_zip(tmp_path, [G204]), "2026-09-14"))
    _gis(monkeypatch, {"0268000000": _gis_row("14555 NE 40TH ST", "BELLEVUE", "98007")})
    asked = _page(monkeypatch, lambda p: None)
    billed = (await db.execute(text("SELECT billed_count, billing_applied_at FROM jobs WHERE id = :j"),
                               {"j": job_id})).first()

    stats = await asyncio.to_thread(_tick)

    assert stats["found_extract"] == 1 and asked == []
    r = await _get(db, rid)
    assert r.property_address == "14527 NE 40TH ST #G204, BELLEVUE, WA 98007"
    assert r.absentee_owner is True                       # flags recomputed from the new address
    assert r.enrichment_data["property_lookup_deferred"] is False
    assert r.enrichment_data["property_lookup_outcome"] == "found"
    assert r.enrichment_data["property_source"] == "king_condo_unit"
    assert r.mailing_address == "806 LAKESHORE DR, REDWOOD, CA 94065" and r.skip_trace_status == "not_attempted"
    assert tuple((await db.execute(text("SELECT billed_count, billing_applied_at FROM jobs WHERE id = :j"),
                                   {"j": job_id})).first()) == tuple(billed)


async def test_the_page_is_asked_with_the_resolved_pin_and_its_answer_is_completed(
    db, business_user, monkeypatch,
):
    job_id = await _job(db, business_user)
    rid = await _lead(db, business_user, job_id, "012603938700",
                      extra={"resolved_parcel_id": "0126039387", "resolved_by": "rpacct_account_number"})
    _gis(monkeypatch, {})
    asked = _page(monkeypatch, lambda p: {"property_address": "302 NW 203RD ST 98177",
                                          "parcel_lookup": "verified"})

    stats = await asyncio.to_thread(_tick)

    assert asked == [(["0126039387"], False)]
    assert stats["found_page"] == 1
    r = await _get(db, rid)
    assert r.parcel_id == "012603938700"
    assert r.property_address == "302 NW 203RD ST 98177" and r.property_zip == "98177"
    assert r.enrichment_data["property_source"] == "king_erealproperty"


async def test_page_outcomes_settle_charge_or_rotate(db, business_user, monkeypatch):
    job_id = await _job(db, business_user)
    none_ = await _lead(db, business_user, job_id, "1000000001")
    mism = await _lead(db, business_user, job_id, "1000000002")
    trans = await _lead(db, business_user, job_id, "1000000003")
    last = await _lead(db, business_user, job_id, "1000000004", extra={"property_recovery_attempts": 4})
    _gis(monkeypatch, {})
    answers = {"1000000001": {"property_address": None, "owner_name": "X", "parcel_lookup": "verified"},
               "1000000002": {"property_address": None, "parcel_lookup": "mismatch"}}
    _page(monkeypatch, lambda p: answers.get(p))

    stats = await asyncio.to_thread(_tick)

    assert (stats["no_site_address"], stats["parcel_mismatch"], stats["transient"], stats["gave_up"]) == (1, 1, 1, 1)
    ed = {k: (await _get(db, v)).enrichment_data for k, v in
          {"none": none_, "mism": mism, "trans": trans, "last": last}.items()}
    assert ed["none"]["property_lookup_outcome"] == "no_site_address" and ed["none"]["property_lookup_deferred"] is False
    assert ed["mism"]["property_lookup_outcome"] == "parcel_mismatch" and ed["mism"]["property_lookup_deferred"] is False
    assert ed["trans"]["property_recovery_attempts"] == 1 and ed["trans"]["property_lookup_deferred"] is True
    assert ed["last"]["property_lookup_outcome"] == "gave_up" and ed["last"]["property_lookup_deferred"] is False

    asked = _page(monkeypatch, lambda p: None)
    await asyncio.to_thread(_tick)
    assert asked == [(["1000000003"], False)]             # settled and given-up parcels are never asked again


async def test_unreached_rows_rotate_uncharged(db, business_user, monkeypatch):
    job_id = await _job(db, business_user)
    rid = await _lead(db, business_user, job_id, "1000000009")
    _gis(monkeypatch, {})

    async def _denied(parcel_ids, **kw):
        kw["stats"].update({"requested_pids": [], "unreached": list(parcel_ids)})
        return {}

    monkeypatch.setattr("src.scrapers.enrichment.king_county_assessor.batch_enrich_king_county", _denied)
    stats = await asyncio.to_thread(_tick)
    assert stats["unreached"] == 1
    ed = (await _get(db, rid)).enrichment_data
    assert "property_recovery_attempts" not in ed and ed["property_recovery_last_at"]
    assert ed["property_lookup_deferred"] is True


async def test_only_marked_delivered_leads_on_done_king_jobs(db, business_user, monkeypatch):
    done = await _job(db, business_user)
    running = await _job(db, business_user, status="enriching")
    pierce = await _job(db, business_user, county="pierce")
    await _lead(db, business_user, done, "2000000001", marked=False)
    await _lead(db, business_user, done, "2000000002", extra={"delivery_excluded_reason": "over_quota"})
    await _lead(db, business_user, done, "2000000003", duplicate=True)
    await _lead(db, business_user, done, "2000000004", prop="1 A ST, KENT, WA 98032")
    await _lead(db, business_user, running, "2000000005")
    await _lead(db, business_user, pierce, "2000000006")
    await _lead(db, business_user, done, "201260393870", extra={})   # 12 digits, never resolved
    _gis(monkeypatch, {})
    asked = _page(monkeypatch, lambda p: None)

    stats = await asyncio.to_thread(_tick)
    assert stats["parcels"] == 0 and asked == []


async def test_kill_switch_and_cooldown(db, business_user, tmp_path, monkeypatch):
    from src.config import settings
    from src.scrapers.enrichment.source_health import record_source_blocked

    job_id = await _job(db, business_user)
    await _lead(db, business_user, job_id, "3000000001")
    _gis(monkeypatch, {})
    asked = _page(monkeypatch, lambda p: None)

    monkeypatch.setattr(settings, "PROPERTY_RECOVERY_ENABLED", False, raising=False)
    assert (await asyncio.to_thread(_tick))["skipped"] == "PROPERTY_RECOVERY_ENABLED is off"
    monkeypatch.setattr(settings, "PROPERTY_RECOVERY_ENABLED", True, raising=False)

    await asyncio.to_thread(record_source_blocked, KING_EREALPROPERTY, "test cooldown")
    stats = await asyncio.to_thread(_tick)
    assert stats["skipped"] == "king_erealproperty is in cooldown" and asked == []


@pytest.mark.parametrize("change, expect_prop", [
    ("property_address = 'FILLED BY A RE-RUN 98001'", "FILLED BY A RE-RUN 98001"),
    # A new mailing address: the owner flags computed from the old one would be wrong.
    ("mailing_address = 'PO BOX 7, KENT, WA 98032'", None),
])
async def test_a_row_changed_meanwhile_is_not_written(db, business_user, monkeypatch, change, expect_prop):
    job_id = await _job(db, business_user)
    rid = await _lead(db, business_user, job_id, "4000000001")
    _gis(monkeypatch, {})

    async def _erp(parcel_ids, **kw):
        kw["stats"]["requested_pids"] = list(parcel_ids)
        await db.execute(text(f"UPDATE results SET {change} WHERE id = :i"), {"i": rid})  # noqa: S608
        await db.commit()
        return {p: {"property_address": "9 WRONG ST 98001", "parcel_lookup": "verified"} for p in parcel_ids}

    monkeypatch.setattr("src.scrapers.enrichment.king_county_assessor.batch_enrich_king_county", _erp)
    stats = await asyncio.to_thread(_tick)
    assert stats["stale"] == 1
    assert (await _get(db, rid)).property_address == expect_prop


async def test_a_job_marks_only_the_leads_no_source_settled(db, business_user, redis_client, monkeypatch):
    """The writing half: after every King pass, unsettled blanks are marked for the sweep."""
    job_id = await _job(db, business_user, status="enriching")
    blank = await _lead(db, business_user, job_id, "5000000001", marked=False)
    page_blank = await _lead(db, business_user, job_id, "5000000002", marked=False, mailing=None)
    filled = await _lead(db, business_user, job_id, "5000000003", marked=False, prop="1 A ST 98001")
    vacant = await _lead(db, business_user, job_id, "5000000004", marked=False,
                         extra={"vacant_no_situs": True})
    _gis(monkeypatch, {})

    async def _erp(parcel_ids, **kw):
        if kw.get("tax_urls_out") is None:
            return {}
        return {"5000000002": {"property_address": None, "owner_name": "X", "parcel_lookup": "verified"}}

    monkeypatch.setattr("src.scrapers.enrichment.king_county_assessor.batch_enrich_king_county", _erp)

    def _go():
        from src.db.session import system_sync_session
        from src.workers.tasks_helpers.enrich import _run_inline_enrichment

        with system_sync_session() as sdb:
            job = sdb.get(Job, job_id)
            _run_inline_enrichment(sdb, job, redis_client, job_id, sdb.get(ScraperConfig, job.scraper_config_id),
                                   summary={})

    await asyncio.to_thread(_go)

    assert (await _get(db, blank)).enrichment_data.get("property_lookup_deferred") is True
    pb = (await _get(db, page_blank)).enrichment_data
    assert pb.get("property_lookup_outcome") == "no_site_address" and "property_lookup_deferred" not in pb
    assert "property_lookup_deferred" not in (await _get(db, filled)).enrichment_data
    assert "property_lookup_deferred" not in (await _get(db, vacant)).enrichment_data


async def test_a_non_condo_page_answer_never_borrows_a_neighbouring_parcels_city(db, business_user, tmp_path,
                                                                                monkeypatch):
    job_id = await _job(db, business_user)
    rid = await _lead(db, business_user, job_id, "6000000020")
    # A real condo unit on the SAME major in the same tick puts that complex in the lookup.
    await _lead(db, business_user, job_id, "6000000010")
    monkeypatch.setattr(kc, "cached_extract", lambda *a, **kw: (_condo_zip(tmp_path, [
        _unit("600000", "0010", "5 UNIT ST #1 98032", "98032", "1")]), "2026-09-14"))
    # major+0000 is a different real parcel here, in the same ZIP: its city must not be taken.
    _gis(monkeypatch, {"6000000000": _gis_row("1 OTHER ST", "KENT", "98032")})
    _page(monkeypatch, lambda p: {"property_address": "17 LOT ST 98032", "parcel_lookup": "verified"})

    await asyncio.to_thread(_tick)
    r = await _get(db, rid)
    assert r.property_address == "17 LOT ST 98032" and r.property_city is None


async def test_a_result_for_a_pin_that_was_not_requested_writes_nothing(db, business_user, monkeypatch):
    job_id = await _job(db, business_user)
    rid = await _lead(db, business_user, job_id, "6000000030")
    _gis(monkeypatch, {})

    async def _erp(parcel_ids, **kw):
        kw["stats"]["requested_pids"] = []
        return {p: {"property_address": "9 CACHED ST 98001", "parcel_lookup": "verified"} for p in parcel_ids}

    monkeypatch.setattr("src.scrapers.enrichment.king_county_assessor.batch_enrich_king_county", _erp)
    stats = await asyncio.to_thread(_tick)
    assert stats["unreached"] == 1
    r = await _get(db, rid)
    assert r.property_address is None and "property_recovery_attempts" not in r.enrichment_data
