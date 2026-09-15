"""King (Seattle SDCI) code violations: owner, parcel and violation fields mean what they say.

Before 2026-09-14 party_name held the case label ("Complaint - 7011 ROOSEVELT WAY NE"),
the violation category existed only inside that label, and the located King PIN never
reached the Parcel ID a customer sees. Only external HTTP answers are substituted here
(SDCI rows, King parcel-layer features, eRealProperty markup), copied from real
responses on 2026-09-14. Matching, gating, serialization, export and DB writes run for real.
"""
from __future__ import annotations

import asyncio
import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from src.api.schemas import ResultRow
from src.db.models import Job, Result, ScraperConfig, User
from src.scrapers import king_wa_code_violation as kcv
from src.scrapers.enrichment import king_county_assessor as kca
from src.scrapers.enrichment import king_parcel_locate as kpl
from src.scrapers.enrichment.skip_trace import build_pending_row_payload
from src.scrapers.king_cv_sources import seattle_sdci
from src.utils.lead_export import build_lead_export_row, resolve_lead_export_columns
from src.utils.located_parcel import located_parcel_id
from src.workers.property_identity import legacy_strong_signature
from src.workers.tasks_helpers.dedup import _collapse_groups

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_king_code_violation_owner.py"
_spec = importlib.util.spec_from_file_location("backfill_king_cv_owner", _SCRIPT)
bko = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bko)

# Real SDCI rows (data.seattle.gov ez4a-iug7).
SDCI_ROOSEVELT = {
    "recordnum": "012954-26CP", "recordtype": "Complaint", "recordtypemapped": "Request",
    "recordtypedesc": "", "description": "tenant says ...", "opendate": "2026-08-23T00:00:00.000",
    "statuscurrent": "Under Investigation", "originaladdress1": "7011 ROOSEVELT WAY NE",
    "originalcity": "SEATTLE", "originalstate": "WA", "originalzip": "98115",
    "latitude": "47.67934", "longitude": "-122.31749",
}
SDCI_VACANT = {
    "recordnum": "011576-26CP", "recordtype": "Complaint", "recordtypemapped": "Request",
    "recordtypedesc": "Vacant Building",
    "description": "Home is vacant. Was previously secured but squatters have gained access",
    "opendate": "2026-08-02T00:00:00.000", "statuscurrent": "Completed",
    "originaladdress1": "2114 E FIR ST", "originalcity": "SEATTLE", "originalstate": "WA",
    "originalzip": "98122", "latitude": "47.60287842", "longitude": "-122.30431240",
}
ROOSEVELT_PARCEL = {"PIN": "9138100481", "ADDR_FULL": "7011 ROOSEVELT WAY NE", "ZIP5": "98115",
                    "PROPTYPE": "C"}
ROOSEVELT_PAGE = ('<tr><td style="font-weight:bold;">Parcel Number</td><td>913810-0481</td></tr>'
                  '<tr><td style="font-weight:bold;">Name</td><td>7011 ROOSEVELT WAY NE LLC 7      </td></tr>')


class _Resp:
    def __init__(self, payload=None, status_code=200, text_body=""):
        self._payload, self.status_code, self.text = payload, status_code, text_body

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def _exact_ed(pin="9138100481", **extra) -> dict:
    return {"source": "seattle_sdci_code_violations", "record_number": "012954-26CP",
            "kc_pin": pin, "kc_pin_status": "matched", "kc_pin_source": "king_gis_point_in_parcel",
            "kc_pin_match": "exact", "latitude": "47.67934", "longitude": "-122.31749", **extra}


# ── Scraper: the source has no owner, so party_name is not invented ────────────

@pytest.mark.asyncio
async def test_scraper_stores_no_label_as_party_and_keeps_the_category(monkeypatch):
    pages = [[SDCI_ROOSEVELT, SDCI_VACANT], []]
    monkeypatch.setattr(seattle_sdci, "safe_get", lambda *a, **kw: _Resp(pages.pop(0)))
    recs = await kcv.KingWACodeViolationScraper(sources=[seattle_sdci.SeattleSDCISource()]).scrape(
        "08/01/2026", "08/31/2026")
    by_case = {r.legal_description: r for r in recs}

    vacant = by_case["011576-26CP"]
    assert vacant.party_name is None
    assert vacant.parcel_id is None
    assert vacant.property_address == "2114 E FIR ST, SEATTLE WA 98122"
    assert vacant.enrichment_data["violation_category"] == "Vacant Building"
    assert vacant.enrichment_data["record_type"] == "Complaint"
    assert vacant.enrichment_data["status"] == "Completed"
    # Complainant free text is PII and never persisted.
    assert "description" not in vacant.enrichment_data
    # No category in the source is stored as none, not as the case kind.
    assert by_case["012954-26CP"].enrichment_data["violation_category"] is None


@pytest.mark.asyncio
async def test_scraper_idempotency_key_is_the_case_not_the_party(monkeypatch):
    async def _scrape():
        pages = [[SDCI_ROOSEVELT, SDCI_VACANT], []]
        monkeypatch.setattr(seattle_sdci, "safe_get", lambda *a, **kw: _Resp(pages.pop(0)))
        return await kcv.KingWACodeViolationScraper(
            sources=[seattle_sdci.SeattleSDCISource()]).scrape("08/01/2026", "08/31/2026")

    first, second = await _scrape(), await _scrape()
    keys = [r.raw_html_hash for r in first]
    assert keys == [r.raw_html_hash for r in second]
    assert len(set(keys)) == 2 and all(len(k) == 32 for k in keys)


# ── Parcel location tiers ──────────────────────────────────────────────────────

def _layer(monkeypatch, *features):
    monkeypatch.setattr(kpl, "safe_get", lambda *a, **kw: _Resp(
        {"features": [{"attributes": f} for f in features]}))


def test_street_and_zip_is_exact(monkeypatch):
    _layer(monkeypatch, ROOSEVELT_PARCEL)
    loc = kpl.locate(47.67934, -122.31749, "7011 ROOSEVELT WAY NE, SEATTLE WA 98115")
    assert (loc.status, loc.pin, loc.match) == ("matched", "9138100481", "exact")


def test_no_zip_in_the_source_is_only_street_level(monkeypatch):
    _layer(monkeypatch, ROOSEVELT_PARCEL)
    loc = kpl.locate(47.67934, -122.31749, "7011 ROOSEVELT WAY NE")
    assert (loc.status, loc.match) == ("matched", "street_only")


def test_a_condominium_complex_parcel_is_never_exact(monkeypatch):
    _layer(monkeypatch, {"PIN": "9903000000", "ADDR_FULL": "1125 N 93RD ST", "ZIP5": "98103",
                         "PROPTYPE": "K"})
    loc = kpl.locate(47.6, -122.3, "1125 N 93RD ST, SEATTLE WA 98103")
    assert (loc.status, loc.match) == ("matched", "condo_complex")


@pytest.mark.parametrize("ed", [
    _exact_ed(kc_pin_match="condo_complex"),
    _exact_ed(kc_pin_match="unconfirmed"),
    {k: v for k, v in _exact_ed().items() if k != "kc_pin_match"},  # located before tiers
    _exact_ed(kc_pin_status="address_mismatch"),
    _exact_ed(kc_pin_source="something_else"),
    _exact_ed(pin="913810048"),
    _exact_ed(kc_pin_match=["exact"]),  # malformed, unhashable
    _exact_ed(kc_pin_match={"tier": "exact"}),
    None,
])
def test_only_exact_or_street_level_locations_are_a_parcel_id(ed):
    assert located_parcel_id(ed) is None


def test_exact_location_is_a_parcel_id():
    assert located_parcel_id(_exact_ed()) == "9138100481"
    assert located_parcel_id(_exact_ed(), exact_only=True) == "9138100481"


def test_street_level_location_is_shown_but_not_exact():
    # Owner decision 2026-09-14: the source gave no ZIP, the street matched one parcel.
    street = _exact_ed(kc_pin_match="street_only")
    assert located_parcel_id(street) == "9138100481"
    assert located_parcel_id(street, exact_only=True) is None


# ── Owner naming ───────────────────────────────────────────────────────────────

def test_owner_is_named_only_for_shown_locations_with_no_party():
    exact = SimpleNamespace(party_name=None, enrichment_data=_exact_ed())
    street = SimpleNamespace(party_name=None, enrichment_data=_exact_ed(
        pin="5249802770", kc_pin_match="street_only"))
    condo = SimpleNamespace(party_name=None, enrichment_data=_exact_ed(
        pin="9903000000", kc_pin_match="condo_complex"))
    named = SimpleNamespace(party_name="SOMEONE ELSE", enrichment_data=_exact_ed())
    pin_map = kpl.owner_lookup_pins([exact, street, condo, named])
    assert pin_map == {"9138100481": [exact], "5249802770": [street]}

    n = kpl.apply_owner_names(pin_map, {"9138100481": "7011 ROOSEVELT WAY NE LLC 7"},
                              checked_at="2026-09-14T00:00:00+00:00")
    assert n == 1
    assert exact.party_name == "7011 ROOSEVELT WAY NE LLC 7"
    assert exact.enrichment_data["owner_source"] == "king_erealproperty"
    assert exact.enrichment_data["owner_pin"] == "9138100481"
    assert street.party_name is None and condo.party_name is None
    assert named.party_name == "SOMEONE ELSE"


def test_an_owner_for_a_different_pin_is_never_applied():
    row = SimpleNamespace(party_name=None, enrichment_data=_exact_ed())
    pin_map = {"9138100481": [row]}
    row.enrichment_data = _exact_ed(pin="1111111111")  # row changed since selection
    assert kpl.apply_owner_names(pin_map, {"9138100481": "X LLC"}, checked_at="t") == 0
    assert row.party_name is None


@pytest.mark.asyncio
async def test_owner_page_for_another_parcel_names_nobody(monkeypatch):
    other = ROOSEVELT_PAGE.replace("913810-0481", "913810-0482")
    monkeypatch.setattr(kca, "safe_get", lambda *a, **kw: _Resp(text_body=other))
    assert await kca._fetch_king_owner("9138100481") == (None, False)


# ── Skip trace: no violation text or empty party is ever paid for ──────────────

def _cv_result(**over):
    base = {"id": "r1", "job_id": "j1", "user_id": "u1", "party_name": None,
                "property_address": "7011 ROOSEVELT WAY NE, SEATTLE WA 98115",
                "mailing_address": "7556 12TH AVE NE, SEATTLE WA 98115",
                "property_city": "SEATTLE", "property_state": "WA", "property_zip": "98115",
                "enrichment_data": _exact_ed()}
    base.update(over)
    return SimpleNamespace(**base)


def test_code_violation_with_no_owner_is_not_traced_even_with_a_blank_party():
    assert build_pending_row_payload(_cv_result()) is None
    assert build_pending_row_payload(_cv_result(party_name="")) is None


def test_code_violation_label_is_not_traced():
    assert build_pending_row_payload(_cv_result(party_name="Complaint - 7011 ROOSEVELT WAY NE")) is None
    tacoma = _cv_result(party_name="Nuisance - 100 MAIN ST",
                        enrichment_data={"source": "tacoma_code_violations"})
    assert build_pending_row_payload(tacoma) is None
    # King owner proof does not make a Tacoma row traceable; Pierce has no owner pass.
    tacoma_king_shaped = _cv_result(party_name="SMITH JOHN A", enrichment_data={
        **_exact_ed(owner_source="king_erealproperty", owner_pin="9138100481"),
        "source": "tacoma_code_violations"})
    assert build_pending_row_payload(tacoma_king_shaped) is None


def test_a_party_name_without_a_county_owner_source_is_not_traced():
    # A name typed into party_name by anything other than owner enrichment is not proof.
    assert build_pending_row_payload(_cv_result(party_name="SMITH JOHN")) is None


def test_code_violation_with_a_county_owner_is_traceable():
    ed = _exact_ed(owner_source="king_erealproperty", owner_pin="9138100481")
    payload = build_pending_row_payload(_cv_result(party_name="SMITH JOHN A", enrichment_data=ed))
    assert payload is not None and payload["property_address"] == "7011 ROOSEVELT WAY NE"


@pytest.mark.parametrize("ed", [
    _exact_ed(owner_source="king_erealproperty", owner_pin="1111111111"),  # owner of another parcel
    _exact_ed(owner_source="typed_in", owner_pin="9138100481"),
    _exact_ed(owner_source="king_erealproperty", owner_pin="9138100481",
              kc_pin_match="street_only"),  # location no longer exact
])
def test_stale_or_foreign_owner_metadata_is_not_traced(ed):
    assert build_pending_row_payload(_cv_result(party_name="SMITH JOHN A", enrichment_data=ed)) is None


def test_other_record_types_are_unaffected():
    row = _cv_result(party_name="SAARENAS AVELINO G", enrichment_data={"source": "king_recorder"})
    assert build_pending_row_payload(row) is not None
    assert build_pending_row_payload(_cv_result(party_name="SAARENAS AVELINO G",
                                                enrichment_data=None)) is not None


# ── API and export ─────────────────────────────────────────────────────────────

def _row(**over) -> dict:
    base = {"id": str(uuid.uuid4()), "date_recorded": "08/23/2026", "party_name": None, "heirs": None,
                "legal_description": "012954-26CP", "parcel_id": None,
                "property_address": "7011 ROOSEVELT WAY NE, SEATTLE WA 98115",
                "mailing_address": "7556 12TH AVE NE, SEATTLE WA 98115",
                "enrichment_data": _exact_ed(violation_category="Vacant Building",
                                          record_type="Complaint", status="Under Investigation"),
                "created_at": "2026-09-13T00:00:00Z"}
    base.update(over)
    return base


def test_api_exposes_the_located_parcel_beside_parcel_id():
    out = ResultRow(**_row())
    assert out.parcel_id is None
    assert out.located_parcel_id == "9138100481"


def test_api_never_shows_a_located_parcel_over_a_real_one():
    out = ResultRow(**_row(parcel_id="1234567890"))
    assert (out.parcel_id, out.located_parcel_id) == ("1234567890", None)


def test_api_shows_a_street_level_location_and_says_so():
    out = ResultRow(**_row(enrichment_data=_exact_ed(kc_pin_match="street_only")))
    assert (out.located_parcel_id, out.located_parcel_match) == ("9138100481", "street_only")
    exact = ResultRow(**_row())
    assert exact.located_parcel_match == "exact"


def test_api_hides_a_condo_complex_location():
    out = ResultRow(**_row(enrichment_data=_exact_ed(kc_pin_match="condo_complex")))
    assert (out.located_parcel_id, out.located_parcel_match) == (None, None)


def test_export_labels_a_street_level_parcel():
    row = build_lead_export_row(_row(enrichment_data=_exact_ed(kc_pin_match="street_only")))
    assert row["parcel_id"] == "9138100481"
    assert row["parcel_source"] == "County parcel map match (street only, no ZIP in source)"


def test_export_fills_parcel_and_says_where_it_came_from():
    row = build_lead_export_row(_row())
    assert row["parcel_id"] == "9138100481"
    assert row["parcel_source"] == "County parcel map match"
    assert row["code_violation_type"] == "Vacant Building"
    assert row["code_violation_status"] == "Under Investigation"
    assert row["party_name"] == ""


def test_export_keeps_a_real_parcel_and_falls_back_to_the_case_kind():
    ed = {"source": "seattle_sdci_code_violations", "record_type": "Complaint"}
    row = build_lead_export_row(_row(parcel_id="1234567890", enrichment_data=ed))
    assert (row["parcel_id"], row["parcel_source"]) == ("1234567890", "")
    assert row["code_violation_type"] == "Complaint"
    tacoma = build_lead_export_row(_row(enrichment_data={"case_type": "Nuisance"}))
    assert (tacoma["code_violation_type"], tacoma["parcel_id"]) == ("Nuisance", "")


def test_parcel_source_column_ships_with_code_violation_exports_only():
    assert "parcel_source" in resolve_lead_export_columns("code_violation")
    assert "parcel_source" not in resolve_lead_export_columns("probate")


# ── Billing identity stays untouched ───────────────────────────────────────────

def test_siblings_still_collapse_after_owner_and_parcel_enrichment():
    addr = "4414 BAKER AVE NW, SEATTLE WA 98107"
    stored = legacy_strong_signature(None, addr)  # computed at insert, parcel NULL
    rows = [{"id": f"r{i}", "parcel_id": None, "property_address": addr, "dedup_hash": stored,
             "party_name": "OWNER NAME", "mailing_address": "7321 121ST DR NE, LAKE STEVENS, WA 98258",
             "enrichment_data": _exact_ed(pin="6610000315"), "created_at": f"2026-08-1{i}"}
            for i in range(3)]
    groups = _collapse_groups(rows)
    assert len(groups) == 1 and len(groups[0][1]) == 2


# ── Historical repair ──────────────────────────────────────────────────────────

async def _stored_row(db, user: User, *, party, address, ed, status="done") -> tuple[str, str]:
    config = ScraperConfig(id=str(uuid.uuid4()), user_id=user.id, name="King CV",
                           county="king", state="WA", record_type="code_violation",
                           fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
                           deliver={"format": "csv", "emails": []})
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status=status,
               trigger="manual", record_count=1, billed_count=1))
    await db.commit()
    rid = str(uuid.uuid4())
    dedup = legacy_strong_signature(None, address)
    db.add(Result(id=rid, user_id=user.id, job_id=job_id, party_name=party, parcel_id=None,
                  property_address=address, legal_description=ed["record_number"],
                  mailing_address="7556 12TH AVE NE, SEATTLE WA 98115", dedup_hash=dedup,
                  skip_trace_status="not_attempted", is_duplicate=False, enrichment_data=ed))
    await db.commit()
    return rid, dedup


@pytest.mark.asyncio
async def test_historical_repair_names_owners_clears_labels_and_converges(
    db, business_user, tmp_path, monkeypatch,
):
    legacy_ed = {k: v for k, v in _exact_ed().items() if k != "kc_pin_match"}
    named, dedup = await _stored_row(db, business_user, party="Complaint - 7011 ROOSEVELT WAY NE",
                                     address="7011 ROOSEVELT WAY NE, SEATTLE WA 98115", ed=legacy_ed)
    vacant_ed = {"source": "seattle_sdci_code_violations", "record_number": "011576-26CP",
                 "kc_pin_status": "address_mismatch", "latitude": "47.60287842",
                 "longitude": "-122.30431240"}
    cleared, _ = await _stored_row(db, business_user, party="Vacant Building - 2114 E FIR ST",
                                   address="2114 E FIR ST, SEATTLE WA 98122", ed=vacant_ed)
    # A party_name the old scraper could not have built from this case is not ours to change.
    foreign, _ = await _stored_row(db, business_user, party="HAND ENTERED NAME",
                                   address="2114 E FIR ST, SEATTLE WA 98122",
                                   ed={**vacant_ed, "record_number": "011576-26CP"})
    live, _ = await _stored_row(db, business_user, party="Complaint - 7011 ROOSEVELT WAY NE",
                                address="7011 ROOSEVELT WAY NE, SEATTLE WA 98115",
                                ed=legacy_ed, status="enriching")

    sdci_calls: list = []

    def _sdci(url, params=None, **kw):
        sdci_calls.append(params["$where"])
        return _Resp([SDCI_ROOSEVELT, SDCI_VACANT])

    import src.utils.safe_http as safe_http
    monkeypatch.setattr(safe_http, "safe_get", _sdci)
    monkeypatch.setattr(bko.time, "sleep", lambda s: None)
    _layer(monkeypatch, ROOSEVELT_PARCEL)
    monkeypatch.setattr(kca, "safe_get", lambda *a, **kw: _Resp(text_body=ROOSEVELT_PAGE))

    async def _no_wait(_s):
        return None

    monkeypatch.setattr(kca.asyncio, "sleep", _no_wait)

    def _run(apply_writes):
        from src.db.session import system_sync_session

        with system_sync_session() as sdb:
            return bko.run(sdb, apply_writes=apply_writes, owners=True,
                           report=tmp_path / "ev.jsonl", socrata_pace_s=0, gis_pace_s=0,
                           owner_delay=0)

    dry = await asyncio.to_thread(_run, False)
    assert dry["candidates"] == 3 and "writes" not in dry
    assert "recordnum in (" in sdci_calls[0]
    stats = await asyncio.to_thread(_run, True)
    assert stats["writes"] == {"written": 3, "skipped_by_write_guard": 0}
    assert stats["named"] == 1 and stats["label_cleared_no_owner"] == 1
    assert stats["party_name_not_the_label_left_alone"] == 1

    got = {str(r.id): r for r in (await db.execute(text(
        "SELECT id, party_name, parcel_id, dedup_hash, mailing_address, skip_trace_status, "
        "enrichment_data FROM results WHERE id = ANY(:ids)"),
        {"ids": [named, cleared, foreign, live]})).all()}
    n = got[named]
    assert n.party_name == "7011 ROOSEVELT WAY NE LLC 7"
    assert n.enrichment_data["owner_source"] == "king_erealproperty"
    assert n.enrichment_data["kc_pin_match"] == "exact"
    assert n.enrichment_data["violation_category"] is None
    assert (n.parcel_id, n.dedup_hash) == (None, dedup)
    assert n.mailing_address == "7556 12TH AVE NE, SEATTLE WA 98115"
    assert n.skip_trace_status == "not_attempted"
    assert got[cleared].party_name is None
    assert got[cleared].enrichment_data["violation_category"] == "Vacant Building"
    assert got[foreign].party_name == "HAND ENTERED NAME"
    assert got[live].party_name == "Complaint - 7011 ROOSEVELT WAY NE"

    again = await asyncio.to_thread(_run, True)
    assert again["candidates"] == 0


@pytest.mark.asyncio
async def test_repair_write_skips_a_row_relocated_since_it_was_read(db, business_user):
    ed = _exact_ed()
    rid, _ = await _stored_row(db, business_user, party="Complaint - 7011 ROOSEVELT WAY NE",
                               address="7011 ROOSEVELT WAY NE, SEATTLE WA 98115", ed=ed)

    def _write(old_pin):
        from src.db.session import system_sync_session

        with system_sync_session() as sdb:
            res = sdb.execute(text(bko._UPDATE_SQL), {
                "new_party": "7011 ROOSEVELT WAY NE LLC 7",
                "old_party": "Complaint - 7011 ROOSEVELT WAY NE", "rid": rid,
                "uid": business_user.id, "payload": "{}", "source": bko._SOURCE,
                "old_pin": old_pin, "old_pin_status": "matched", "old_pin_match": "exact",
                "old_pin_source": "king_gis_point_in_parcel"})
            sdb.commit()
            return res.rowcount

    assert await asyncio.to_thread(_write, "2222222222") == 0
    assert await asyncio.to_thread(_write, "9138100481") == 1


@pytest.mark.asyncio
async def test_a_live_king_cv_job_locates_the_parcel_names_the_owner_and_keeps_parcel_id_null(
    db, business_user, redis_client, tmp_path, monkeypatch,
):
    """Behavioral: the real inline enrichment pass for an enriching King code-violation job."""
    from tests.test_king_rpacct_mailing import _acct, _extract, _use_extract

    base_ed = {"source": "seattle_sdci_code_violations", "record_type": "Complaint",
               "latitude": "47.67934", "longitude": "-122.31749"}
    exact, dedup = await _stored_row(db, business_user, party=None, status="enriching",
                                     address="7011 ROOSEVELT WAY NE, SEATTLE WA 98115",
                                     ed={**base_ed, "record_number": "012954-26CP"})
    job_id = (await db.execute(text("SELECT job_id FROM results WHERE id = :i"),
                               {"i": exact})).scalar()
    # Same job: no ZIP in the source address -> street-only match, no owner lookup.
    street = str(uuid.uuid4())
    db.add(Result(id=street, user_id=business_user.id, job_id=job_id, party_name=None,
                  property_address="7011 ROOSEVELT WAY NE", legal_description="012955-26CP",
                  mailing_address=None, skip_trace_status="not_attempted", is_duplicate=False,
                  enrichment_data={**base_ed, "record_number": "012955-26CP"}))
    await db.execute(text("UPDATE results SET mailing_address = NULL WHERE id = :i"), {"i": exact})
    await db.commit()

    _layer(monkeypatch, ROOSEVELT_PARCEL)
    monkeypatch.setattr(kpl.time, "sleep", lambda s: None)
    _use_extract(monkeypatch, _extract(tmp_path, [
        _acct("913810", "0481", "7556 12TH AVE NE", "SEATTLE WA", "98115")]))
    owner_pages: list = []

    def _erp(url, *a, **kw):
        owner_pages.append(url)
        return _Resp(text_body=ROOSEVELT_PAGE)

    monkeypatch.setattr(kca, "safe_get", _erp)

    async def _no_wait(_s):
        return None

    monkeypatch.setattr(kca.asyncio, "sleep", _no_wait)
    monkeypatch.setattr("src.scrapers.enrichment.county_gis.batch_enrich_parcels_gis",
                        lambda *a, **kw: {})

    def _go():
        from src.db.session import system_sync_session
        from src.workers.tasks_helpers.enrich import _run_inline_enrichment

        with system_sync_session() as sdb:
            job = sdb.get(Job, str(job_id))
            config = sdb.get(ScraperConfig, job.scraper_config_id)
            _run_inline_enrichment(sdb, job, redis_client, str(job_id), config, summary={})

    await asyncio.to_thread(_go)

    got = {str(r.id): r for r in (await db.execute(text(
        "SELECT id, party_name, parcel_id, dedup_hash, mailing_address, enrichment_data "
        "FROM results WHERE id = ANY(:ids)"), {"ids": [exact, street]})).all()}
    e, s = got[exact], got[street]
    assert e.party_name == "7011 ROOSEVELT WAY NE LLC 7"
    assert e.enrichment_data["owner_source"] == "king_erealproperty"
    assert e.enrichment_data["owner_pin"] == "9138100481"
    assert e.enrichment_data["kc_pin_match"] == "exact"
    assert e.mailing_address == "7556 12TH AVE NE, SEATTLE, WA 98115"
    assert (e.parcel_id, e.dedup_hash) == (None, dedup)
    assert located_parcel_id(e.enrichment_data) == "9138100481"
    assert s.enrichment_data["kc_pin_match"] == "street_only"
    # Street-level match: shown and named (owner decision), parcel_id still untouched.
    assert s.party_name == "7011 ROOSEVELT WAY NE LLC 7" and s.parcel_id is None
    assert located_parcel_id(s.enrichment_data) == "9138100481"
    assert located_parcel_id(s.enrichment_data, exact_only=True) is None
    assert len(owner_pages) == 1  # both rows sit on one PIN: one page


@pytest.mark.asyncio
async def test_retry_owners_names_street_level_rows_and_never_condo_complexes(
    db, business_user, tmp_path, monkeypatch,
):
    repaired = {"cv_semantics_repaired_at": "2026-09-14T15:30:00+00:00",
                "violation_category": None}
    street, dedup = await _stored_row(
        db, business_user, party=None, address="7011 ROOSEVELT WAY NE",
        ed={**_exact_ed(kc_pin_match="street_only"), **repaired})
    condo, _ = await _stored_row(
        db, business_user, party=None, address="7011 ROOSEVELT WAY NE, SEATTLE WA 98115",
        ed={**_exact_ed(pin="9903000000", kc_pin_match="condo_complex"), **repaired})
    import src.utils.safe_http as safe_http
    monkeypatch.setattr(safe_http, "safe_get", lambda *a, **kw: _Resp([SDCI_ROOSEVELT]))
    monkeypatch.setattr(bko.time, "sleep", lambda s: None)
    pages: list = []

    def _erp(url, *a, **kw):
        pages.append(url)
        return _Resp(text_body=ROOSEVELT_PAGE)

    monkeypatch.setattr(kca, "safe_get", _erp)

    async def _no_wait(_s):
        return None

    monkeypatch.setattr(kca.asyncio, "sleep", _no_wait)

    def _run(retry):
        from src.db.session import system_sync_session

        with system_sync_session() as sdb:
            return bko.run(sdb, apply_writes=True, owners=True, retry_owners=retry,
                           report=tmp_path / "r.jsonl", socrata_pace_s=0, gis_pace_s=0,
                           owner_delay=0)

    assert (await asyncio.to_thread(_run, False))["candidates"] == 0
    stats = await asyncio.to_thread(_run, True)
    assert stats["candidates"] == 1 and stats["named"] == 1
    assert len(pages) == 1 and pages[0].endswith("9138100481")

    got = {str(r.id): r for r in (await db.execute(text(
        "SELECT id, party_name, parcel_id, dedup_hash, enrichment_data FROM results "
        "WHERE id = ANY(:ids)"), {"ids": [street, condo]})).all()}
    assert got[street].party_name == "7011 ROOSEVELT WAY NE LLC 7"
    assert got[street].enrichment_data["owner_pin"] == "9138100481"
    assert (got[street].parcel_id, got[street].dedup_hash) == (None, dedup)
    assert got[condo].party_name is None and "owner_source" not in got[condo].enrichment_data
    # A named street-level row is never paid for.
    row = SimpleNamespace(party_name=got[street].party_name, enrichment_data=got[street].enrichment_data,
                          property_address="7011 ROOSEVELT WAY NE", mailing_address=None,
                          property_city="SEATTLE", property_state="WA", property_zip=None,
                          id="x", job_id="y", user_id="z")
    assert build_pending_row_payload(row) is None
    assert (await asyncio.to_thread(_run, True))["candidates"] == 0


def test_repair_refuses_owner_lookups_on_the_private_redis_host(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://default:x@redis.railway.internal:6379")
    with pytest.raises(SystemExit):
        bko._refuse_private_redis()


def test_old_label_is_rebuilt_exactly_from_the_source_row():
    assert bko.old_scraper_label(SDCI_VACANT) == "Vacant Building - 2114 E FIR ST"
    assert bko.old_scraper_label(SDCI_ROOSEVELT) == "Complaint - 7011 ROOSEVELT WAY NE"
    assert bko.old_scraper_label({"recordtype": "Complaint"}) == "Complaint"
