"""King code violations from Seattle, Bellevue, Burien and King County Accela behind one connector.

Only external HTTP answers are substituted: ArcGIS payloads are the real responses saved
under tests/fixtures/king_cv_*.json (fetched 2026-09-14), the eRealProperty rows and the
Assessor extract row are copied from the real pages/file for the same parcels. Parsing,
paging, gating, owner naming, enrichment and DB writes run for real.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import requests
from sqlalchemy import text

from src.config import settings
from src.db.models import Job, Result, ScraperConfig
from src.scrapers import king_wa_code_violation as kcv
from src.scrapers.enrichment import king_county_assessor as kca
from src.scrapers.enrichment import king_parcel_locate as kpl
from src.scrapers.enrichment.skip_trace import build_pending_row_payload
from src.scrapers.king_cv_sources import base, bellevue, burien, kingco_accela, seattle_sdci
from src.workers.property_identity import legacy_strong_signature

_FIX = Path(__file__).resolve().parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((_FIX / name).read_text(encoding="utf-8"))


BEL_LAYERS = _fixture("king_cv_bellevue_layers.json")["layers"]
BEL_QUERY = _fixture("king_cv_bellevue_query.json")
BEL_PAGED = _fixture("king_cv_bellevue_paged.json")
BUR_PAGED = _fixture("king_cv_burien_paged.json")
ARCGIS_ERROR = _fixture("king_cv_arcgis_error.json")

# Real SDCI row (data.seattle.gov ez4a-iug7), as in test_king_code_violation_semantics.
SDCI_ROW = {
    "recordnum": "011576-26CP", "recordtype": "Complaint", "recordtypedesc": "Vacant Building",
    "opendate": "2026-08-02T00:00:00.000", "statuscurrent": "Completed",
    "originaladdress1": "2114 E FIR ST", "originalcity": "SEATTLE", "originalstate": "WA",
    "originalzip": "98122", "latitude": "47.60287842", "longitude": "-122.30431240",
}

# Real eRealProperty rows and Assessor extract rows for two fixture parcels (2026-09-14).
ERP_BELLEVUE = ('<tr class="GridViewRowStyle"><td style="font-weight:bold;">Parcel Number</td>'
                '<td>257120-0050</td></tr><tr class="GridViewAlternatingRowStyle">'
                '<td style="font-weight:bold;">Name</td><td>DVD SE 13TH PL LLC               </td></tr>')
ERP_BURIEN = ('<tr class="GridViewRowStyle"><td style="font-weight:bold;">Parcel Number</td>'
              '<td>783580-0148</td></tr><tr class="GridViewAlternatingRowStyle">'
              '<td style="font-weight:bold;">Name</td><td>OVERLOOK AT BURIEN LLC           </td></tr>')


class _Resp:
    def __init__(self, payload=None, status_code=200, text_body=""):
        self._payload, self.status_code, self.text = payload, status_code, text_body

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)


def _hash(source: str, case: str) -> str:
    return hashlib.sha256(f"{source}|{case}".encode()).hexdigest()[:32]


class _ArcGIS:
    """Answers base.safe_get from the saved responses and records every request."""

    def __init__(self, *, bellevue_query=None, burien_pages=None, layers=None, fail=()):
        self.calls: list[tuple[str, dict]] = []
        self.bellevue_query = bellevue_query
        self.burien_pages = burien_pages
        self.layers = layers if layers is not None else BEL_LAYERS
        self.fail = set(fail)

    def __call__(self, url, params=None, **kw):
        params = dict(params or {})
        self.calls.append((url, params))
        if "burienwa" in url:
            if "burien" in self.fail:
                return _Resp(status_code=503)
            return _Resp(self.burien_pages["pages"][str(params["resultOffset"])])
        if "Bellevue" in url:
            if "bellevue" in self.fail:
                return _Resp(status_code=503)
            service = url.split("/services/")[1].split("/")[0]
            if not url.endswith("/query"):
                layer = self.layers[service]
                return _Resp(layer) if layer is not None else _Resp(ARCGIS_ERROR)
            return _Resp(self.bellevue_query(params))
        raise AssertionError(f"unexpected request {url}")


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(base.time, "sleep", lambda s: None)
    monkeypatch.setattr(seattle_sdci.time, "sleep", lambda s: None)


def _bellevue_full(params):
    assert params["resultOffset"] == 0
    return BEL_QUERY["response"]


# ── Parcel normalization ──────────────────────────────────────────────────────

@pytest.mark.parametrize(("raw", "pin"), [
    ("3361400395", "3361400395"),
    ("336140-0395", "3361400395"),
    (" 3361400395 ", "3361400395"),
    ("0123456789", "0123456789"),  # leading zero kept, never treated as a number
    (123456789, None),  # an int already lost its leading zero: not provable
    ("336140039", None),
    ("33614003951", None),
    ("33614OO395", None),
    ("", None),
    (None, None),
])
def test_king_pin_normalization(raw, pin):
    assert base.normalize_king_pin(raw) == pin


# ── Bellevue ─────────────────────────────────────────────────────────────────

def test_bellevue_queries_the_more_recently_edited_service(monkeypatch, no_backoff):
    arc = _ArcGIS()
    monkeypatch.setattr(base, "safe_get", arc)
    assert bellevue.BellevueSource().pick_service() == "Bellevue_Permits"

    older_first = json.loads(json.dumps(BEL_LAYERS))
    older_first["Bellevue_Permits"]["editingInfo"]["dataLastEditDate"] = 1
    monkeypatch.setattr(base, "safe_get", _ArcGIS(layers=older_first))
    assert bellevue.BellevueSource().pick_service() == "Bellevue_Permit"


def test_bellevue_uses_the_other_service_when_one_is_down_and_fails_when_both_are(
        monkeypatch, no_backoff):
    one_down = {**BEL_LAYERS, "Bellevue_Permits": None}  # real ArcGIS error body
    arc = _ArcGIS(layers=one_down)
    monkeypatch.setattr(base, "safe_get", arc)
    assert bellevue.BellevueSource().pick_service() == "Bellevue_Permit"
    # The error body is retried like a throttle before the copy is given up on.
    assert sum(1 for u, _ in arc.calls if "Bellevue_Permits/" in u) == settings.MAX_RETRIES

    monkeypatch.setattr(base, "safe_get", _ArcGIS(layers={"Bellevue_Permits": None,
                                                          "Bellevue_Permit": None}))
    with pytest.raises(RuntimeError, match="neither Bellevue permit service"):
        bellevue.BellevueSource().pick_service()


@pytest.mark.parametrize("stamp", ["missing", None, "1786950000000", 0, True])
def test_a_bellevue_copy_without_a_data_edit_date_is_never_picked(monkeypatch, stamp):
    undated = json.loads(json.dumps(BEL_LAYERS))
    if stamp == "missing":
        del undated["Bellevue_Permits"]["editingInfo"]["dataLastEditDate"]
    else:
        undated["Bellevue_Permits"]["editingInfo"]["dataLastEditDate"] = stamp
    monkeypatch.setattr(base, "safe_get", _ArcGIS(layers=undated))
    assert bellevue.BellevueSource().pick_service() == "Bellevue_Permit"

    del undated["Bellevue_Permit"]["editingInfo"]
    monkeypatch.setattr(base, "safe_get", _ArcGIS(layers=undated))
    with pytest.raises(RuntimeError, match="with a data edit date"):
        bellevue.BellevueSource().pick_service()


@pytest.mark.asyncio
async def test_bellevue_parses_real_cases_with_the_pin_as_parcel_id(monkeypatch):
    arc = _ArcGIS(bellevue_query=_bellevue_full)
    monkeypatch.setattr(base, "safe_get", arc)
    recs = await bellevue.BellevueSource().fetch("07/14/2026", "09/14/2026")

    query = [p for u, p in arc.calls if u.endswith("/query")]
    assert query[0]["where"] == BEL_QUERY["where"]
    assert query[0]["orderByFields"] == "ObjectId ASC"
    assert "OWNER" not in query[0]["outFields"]
    assert len(recs) == len(BEL_QUERY["response"]["features"]) == 148

    by_case = {r.legal_description: r for r in recs}
    dvd = by_case["26 116940 EA"]
    assert dvd.parcel_id == "2571200050"
    assert dvd.property_address == "10202 SE 13th Pl, Bellevue WA 98004"
    assert dvd.date_recorded == "07/16/2026"
    assert dvd.party_name is None
    assert dvd.raw_html_hash == _hash("bellevue_code_enforcement", "26 116940 EA")
    ed = dvd.enrichment_data
    assert ed["source"] == "bellevue_code_enforcement"
    assert ed["case_number"] == "26 116940 EA"
    assert ed["status"] == "Open"
    assert ed["violation_category"] == "Clearing & Grading"
    assert ed["applied_at"] == "2026-07-16T00:00:00-07:00"
    assert ed["source_service"] == "Bellevue_Permits"
    assert "source_owner" not in ed and "OWNER" not in ed

    # A case with no PIN or no ZIP in the source is kept, without inventing either.
    no_pin = by_case["26 118662 EA"]
    assert (no_pin.parcel_id, no_pin.property_address) == (None, None)
    assert by_case["26 120347 EA"].property_address == "14635 NE 32nd St, Bellevue WA"
    assert all(r.parcel_id is None or (len(r.parcel_id) == 10 and r.parcel_id.isdigit())
               for r in recs)


@pytest.mark.asyncio
async def test_bellevue_pages_by_object_id_until_a_short_page(monkeypatch):
    monkeypatch.setattr(bellevue, "_PAGE_SIZE", BEL_PAGED["page_size"])
    arc = _ArcGIS(bellevue_query=lambda p: BEL_PAGED["pages"][str(p["resultOffset"])])
    monkeypatch.setattr(base, "safe_get", arc)
    recs = await bellevue.BellevueSource().fetch("09/01/2026", "09/14/2026")

    query = [p for u, p in arc.calls if u.endswith("/query")]
    assert [p["resultOffset"] for p in query] == [0, 3, 6, 9, 12, 15]
    assert {p["where"] for p in query} == {BEL_PAGED["where"]}
    assert len(recs) == 17 == len({r.legal_description for r in recs})


@pytest.mark.asyncio
async def test_a_server_that_caps_pages_below_the_page_size_skips_nothing(monkeypatch):
    # The same real features, served 2 at a time (below the requested 3) with the
    # server's exceededTransferLimit flag while more remain.
    monkeypatch.setattr(bellevue, "_PAGE_SIZE", BEL_PAGED["page_size"])
    pages = BEL_PAGED["pages"]
    every = [f for off in sorted(pages, key=int) for f in pages[off]["features"]]
    template = pages["0"]

    def _capped(p):
        off = p["resultOffset"]
        chunk = every[off:off + 2]
        return {**template, "features": chunk, "exceededTransferLimit": off + 2 < len(every)}

    arc = _ArcGIS(bellevue_query=_capped)
    monkeypatch.setattr(base, "safe_get", arc)
    recs = await bellevue.BellevueSource().fetch("09/01/2026", "09/14/2026")
    assert len(recs) == 17
    assert [p["resultOffset"] for u, p in arc.calls if u.endswith("/query")][:3] == [0, 2, 4]


@pytest.mark.asyncio
async def test_an_empty_page_that_flags_more_rows_fails_the_source(monkeypatch, no_backoff):
    monkeypatch.setattr(bellevue, "_PAGE_SIZE", BEL_PAGED["page_size"])
    pages = BEL_PAGED["pages"]

    def _answer(p):
        if p["resultOffset"] == 3:
            return {**pages["3"], "features": [], "exceededTransferLimit": True}
        return pages[str(p["resultOffset"])]

    monkeypatch.setattr(base, "safe_get", _ArcGIS(bellevue_query=_answer))
    with pytest.raises(RuntimeError, match="empty page at offset 3"):
        await bellevue.BellevueSource().fetch("09/01/2026", "09/14/2026")


@pytest.mark.asyncio
async def test_bellevue_window_is_the_pacific_day_and_both_edges_are_kept(monkeypatch):
    monkeypatch.setattr(base, "safe_get", _ArcGIS(bellevue_query=_bellevue_full))
    recs = await bellevue.BellevueSource().fetch("07/16/2026", "07/20/2026")
    days = sorted({r.date_recorded for r in recs})
    assert days[0] == "07/16/2026" and days[-1] == "07/20/2026"
    in_window = [f for f in BEL_QUERY["response"]["features"]
                 if "07/16/2026" <= datetime.fromtimestamp(
                     f["attributes"]["APPLIEDDATE"] / 1000, UTC).astimezone(
                         ZoneInfo("America/Los_Angeles")).strftime("%m/%d/%Y") <= "07/20/2026"]
    assert len(recs) == len(in_window)


def test_bellevue_dates_are_read_in_pacific_time_not_utc():
    # Stored as local midnight (07:00Z). A late-evening case is still that Pacific day,
    # although it is already the next day in UTC.
    assert bellevue._epoch_ms_to_local(1786950000000).strftime("%m/%d/%Y") == "08/17/2026"
    late = datetime(2026, 8, 18, 6, 30, tzinfo=UTC).timestamp() * 1000
    assert bellevue._epoch_ms_to_local(late).strftime("%m/%d/%Y") == "08/17/2026"
    assert bellevue._epoch_ms_to_local("08/17/2026") is None
    assert bellevue._epoch_ms_to_local(True) is None


# ── Burien ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_burien_pages_deterministically_and_dedupes_by_case(monkeypatch):
    monkeypatch.setattr(burien, "_PAGE_SIZE", BUR_PAGED["page_size"])
    arc = _ArcGIS(burien_pages=BUR_PAGED)
    monkeypatch.setattr(base, "safe_get", arc)
    recs = await burien.BurienSource().fetch("01/01/2026", "12/31/2026")

    assert [p["resultOffset"] for _, p in arc.calls] == [0, 40, 80]
    assert {p["where"] for _, p in arc.calls} == {BUR_PAGED["where"]}
    # OBJECTID repeats in the real layer, so it cannot be the only sort key.
    rows = [f["attributes"] for page in BUR_PAGED["pages"].values() for f in page["features"]]
    assert len({a["OBJECTID"] for a in rows}) < len(rows)
    assert {p["orderByFields"] for _, p in arc.calls} == {"OBJECTID ASC,CaseNumber ASC"}
    assert len(recs) == len(rows) == len({r.legal_description for r in recs}) == 108

    glendale = next(r for r in recs if r.legal_description == "CE26-02247")
    assert glendale.parcel_id == "3361400395"
    assert glendale.property_address == "11271 Glendale Way S, Burien WA"
    assert glendale.date_recorded == "08/31/2026"
    assert glendale.party_name is None
    assert glendale.raw_html_hash == _hash("burien_code_enforcement", "CE26-02247")
    assert glendale.enrichment_data == {
        "source": "burien_code_enforcement", "case_number": "CE26-02247", "case_id": 78903,
        "status": "VIOLATION", "violation_category": None, "case_type": "Code Enforcement",
        "applied_date": "08/31/2026", "source_parcel_number": "3361400395"}


@pytest.mark.asyncio
async def test_burien_window_edges_are_inclusive(monkeypatch):
    monkeypatch.setattr(burien, "_PAGE_SIZE", BUR_PAGED["page_size"])
    monkeypatch.setattr(base, "safe_get", _ArcGIS(burien_pages=BUR_PAGED))
    recs = await burien.BurienSource().fetch("08/12/2026", "08/31/2026")
    cases = {r.legal_description for r in recs}
    assert {"CE26-02109", "CE26-02247"} <= cases  # applied 08/12 and 08/31
    assert all("08/12/2026" <= r.date_recorded <= "08/31/2026" for r in recs)
    assert not any(r.date_recorded == "08/11/2026" for r in recs)


@pytest.mark.asyncio
async def test_burien_year_filter_covers_a_window_across_new_year(monkeypatch):
    arc = _ArcGIS(burien_pages={"pages": {"0": {"features": []}}})
    monkeypatch.setattr(base, "safe_get", arc)
    assert await burien.BurienSource().fetch("12/15/2025", "01/10/2026") == []
    assert arc.calls[0][1]["where"] == (
        "CaseType='Code Enforcement' AND "
        "(AppliedDate LIKE '%/2025' OR AppliedDate LIKE '%/2026')")


@pytest.mark.parametrize(("raw", "address"), [
    ("1822 SW 152ND ST,  BURIEN,  98166", "1822 SW 152ND ST, Burien WA 98166"),
    ("646 SW 139TH ST,  BURIEN,  WA,  98166", "646 SW 139TH ST, Burien WA 98166"),
    ("11848 12th Ave S Burien, WA 98168", "11848 12th Ave S, Burien WA 98168"),
    ("13007 12th Ave SW", "13007 12th Ave SW, Burien WA"),
    ("", None),
    (None, None),
])
def test_burien_address_shapes(raw, address):
    assert burien.normalize_address(raw) == address


# ── Canaries ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bellevue_canary_fails_loud_on_a_date_format_change(monkeypatch):
    changed = json.loads(json.dumps(BEL_QUERY["response"]))
    for f in changed["features"]:
        f["attributes"]["APPLIEDDATE"] = "07/16/2026"
    monkeypatch.setattr(base, "safe_get", _ArcGIS(bellevue_query=lambda p: changed))
    with pytest.raises(RuntimeError, match="none parsed"):
        await bellevue.BellevueSource().fetch("07/14/2026", "09/14/2026")


@pytest.mark.asyncio
async def test_burien_canary_fails_on_a_field_rename_but_not_on_an_empty_window(monkeypatch):
    monkeypatch.setattr(burien, "_PAGE_SIZE", BUR_PAGED["page_size"])
    renamed = json.loads(json.dumps(BUR_PAGED))
    for page in renamed["pages"].values():
        for f in page["features"]:
            f["attributes"]["CaseNo"] = f["attributes"].pop("CaseNumber")
    monkeypatch.setattr(base, "safe_get", _ArcGIS(burien_pages=renamed))
    with pytest.raises(RuntimeError, match="none parsed"):
        await burien.BurienSource().fetch("01/01/2026", "12/31/2026")

    monkeypatch.setattr(base, "safe_get", _ArcGIS(burien_pages=BUR_PAGED))
    # The year query still returns all 108 cases of 2026; none fall in December yet.
    assert await burien.BurienSource().fetch("12/01/2026", "12/31/2026") == []


@pytest.mark.asyncio
async def test_an_arcgis_error_body_is_a_failure_not_zero_cases(monkeypatch, no_backoff):
    monkeypatch.setattr(base, "safe_get", _ArcGIS(bellevue_query=lambda p: ARCGIS_ERROR))
    with pytest.raises(RuntimeError, match="Invalid query parameters"):
        await bellevue.BellevueSource().fetch("09/01/2026", "09/14/2026")


@pytest.mark.asyncio
@pytest.mark.parametrize("features", [None, "none", [None], [["attributes"]], [{"attributes": None}],
                                      [{"attributes": []}], [{"geometry": {}}]])
async def test_a_query_answer_whose_features_are_not_a_list_of_objects_fails(
    monkeypatch, no_backoff, features,
):
    # The real query answer with only its features value broken.
    broken = {**BEL_QUERY["response"], "features": features}
    monkeypatch.setattr(base, "safe_get", _ArcGIS(bellevue_query=lambda p: broken))
    with pytest.raises(RuntimeError, match="malformed body"):
        await bellevue.BellevueSource().fetch("07/16/2026", "07/20/2026")


# ── One connector over every source ──────────────────────────────────────────

def _connector(monkeypatch, *, fail=(), sdci_fails=False):
    from tests.test_king_cv_kingco_accela import (
        SINGLE_RESULT,
        SINGLE_RESULT_URL,
        _snap,
        use_fixture_portal,
    )

    # King County Accela answers from its real saved pages: the one-case detail page for
    # the last window (and its 09/13 day), the real no-results page for every other one.
    one_case = [_snap(SINGLE_RESULT, SINGLE_RESULT_URL)]
    accela_fail = ([kingco_accela.AccelaAccessWallError("login page")] if "accela" in fail
                   else [kingco_accela.AccelaBudgetError("more than 200 cases")] if "accela_budget" in fail
                   else [])
    use_fixture_portal(monkeypatch, fail=accela_fail, searches={
        (date(2026, 9, 12), date(2026, 9, 14)): one_case, (date(2026, 9, 13), date(2026, 9, 13)): one_case})
    monkeypatch.setattr(burien, "_PAGE_SIZE", BUR_PAGED["page_size"])
    monkeypatch.setattr(base, "safe_get", _ArcGIS(
        bellevue_query=_bellevue_full, burien_pages=BUR_PAGED, fail=fail))
    sdci = (lambda *a, **kw: _Resp(status_code=503)) if sdci_fails else (lambda *a, **kw: _Resp([SDCI_ROW]))
    monkeypatch.setattr(seattle_sdci, "safe_get", sdci)


def test_scope_note_lists_every_registered_jurisdiction():
    scope = kcv.KingWACodeViolationScraper.collection_scope("code_violation")
    assert scope.kind == "dataset"
    assert scope.note == ("Collected from the code enforcement records of Seattle, Bellevue, Burien, "
                          "and unincorporated King County.")
    assert "—" not in scope.note
    assert kcv.KingWACodeViolationScraper.collection_scope("probate") is None
    assert kcv.scope_note(kcv.SOURCES[:3]) == (
        "Collected from the code enforcement records of Seattle, Bellevue, and Burien.")
    assert kcv.scope_note([SimpleNamespace(jurisdiction="Seattle")]) == (
        "Collected from the code enforcement records of Seattle.")


@pytest.mark.asyncio
async def test_all_sources_ship_together_each_keeping_its_identity(monkeypatch):
    _connector(monkeypatch)
    scraper = kcv.KingWACodeViolationScraper()
    recs = await scraper.scrape("08/01/2026", "09/14/2026")

    sources = {r.enrichment_data["source"] for r in recs}
    assert sources == {"seattle_sdci_code_violations", "bellevue_code_enforcement",
                       "burien_code_enforcement", "kingco_accela_code_enforcement"}
    assert scraper.source_status == dict.fromkeys(sources, "ok")
    assert scraper.scrape_warnings == []
    sdci = next(r for r in recs if r.enrichment_data["source"] == "seattle_sdci_code_violations")
    # SDCI identity is exactly what it was before the move.
    assert sdci.raw_html_hash == _hash("seattle_sdci", "011576-26CP") and sdci.parcel_id is None
    assert len({r.raw_html_hash for r in recs}) == len(recs)

    again = await kcv.KingWACodeViolationScraper().scrape("08/01/2026", "09/14/2026")
    assert [r.raw_html_hash for r in again] == [r.raw_html_hash for r in recs]


@pytest.mark.asyncio
async def test_one_failed_source_ships_the_rest_with_a_warning_naming_it(monkeypatch, no_backoff):
    _connector(monkeypatch, fail={"bellevue"})
    scraper = kcv.KingWACodeViolationScraper()
    recs = await scraper.scrape("08/01/2026", "09/14/2026")

    assert {r.enrichment_data["source"] for r in recs} == {"seattle_sdci_code_violations",
                                                           "burien_code_enforcement",
                                                           "kingco_accela_code_enforcement"}
    assert scraper.source_status == {"seattle_sdci_code_violations": "ok",
                                     "bellevue_code_enforcement": "failed",
                                     "burien_code_enforcement": "ok",
                                     "kingco_accela_code_enforcement": "ok"}
    assert scraper.scrape_warnings == [
        "Code violation records from Bellevue could not be collected this run, so these leads "
        "cover Seattle, Burien, and unincorporated King County only. Run this scraper again "
        "later to include Bellevue."]
    assert "—" not in scraper.scrape_warnings[0]


@pytest.mark.asyncio
async def test_a_source_over_its_case_limit_is_told_to_use_a_shorter_range_not_to_retry(
        monkeypatch, no_backoff):
    _connector(monkeypatch, fail={"accela_budget"})
    scraper = kcv.KingWACodeViolationScraper()
    await scraper.scrape("08/01/2026", "09/14/2026")
    assert scraper.source_status["kingco_accela_code_enforcement"] == "failed"
    assert scraper.scrape_warnings == [
        "Code violation records from unincorporated King County could not be collected this run, "
        "so these leads cover Seattle, Bellevue, and Burien only. This date range has more "
        "unincorporated King County cases than one run can collect, so use a shorter date range "
        "to include them."]
    assert "later" not in scraper.scrape_warnings[0] and "—" not in scraper.scrape_warnings[0]

    both = kcv.partial_failure_warning(["Bellevue"], ["Seattle"], ["unincorporated King County"])
    assert both == (
        "Code violation records from Bellevue and unincorporated King County could not be "
        "collected this run, so these leads cover Seattle only. Run this scraper again later to "
        "include Bellevue. This date range has more unincorporated King County cases than one run "
        "can collect, so use a shorter date range to include them.")


@pytest.mark.asyncio
async def test_every_source_failing_fails_the_scrape(monkeypatch, no_backoff):
    _connector(monkeypatch, fail={"bellevue", "burien", "accela"}, sdci_fails=True)
    scraper = kcv.KingWACodeViolationScraper()
    with pytest.raises(RuntimeError, match="every source failed") as err:
        await scraper.scrape("08/01/2026", "09/14/2026")
    for key in ("seattle_sdci_code_violations", "bellevue_code_enforcement", "burien_code_enforcement",
                "kingco_accela_code_enforcement"):
        assert key in str(err.value)
    assert set(scraper.source_status.values()) == {"failed"}


async def _source_alerts(db, since: datetime) -> list[tuple[str, str]]:
    rows = (await db.execute(text(
        "SELECT path, detail FROM audit_events WHERE event = 'ops_alert' AND path LIKE :p "
        "AND created_at >= :since ORDER BY path"),
        {"p": f"{kcv.SOURCE_FAILURE_ALERT_KIND}:%", "since": since})).all()
    return [(r.path, r.detail) for r in rows]


@pytest.mark.asyncio
async def test_a_failed_source_leaves_one_ops_alert_naming_it_and_no_error_text(
        db, monkeypatch, no_backoff):
    since = datetime.now(UTC)
    _connector(monkeypatch, fail={"bellevue"})
    await kcv.KingWACodeViolationScraper().scrape("08/01/2026", "09/14/2026")

    alerts = await _source_alerts(db, since)
    assert [path for path, _ in alerts] == [f"{kcv.SOURCE_FAILURE_ALERT_KIND}:bellevue_code_enforcement"]
    assert alerts[0][1].endswith("King code violation source failed: Bellevue")


@pytest.mark.asyncio
async def test_a_range_too_large_for_a_source_is_not_an_ops_alert(db, monkeypatch, no_backoff):
    since = datetime.now(UTC)
    _connector(monkeypatch, fail={"accela_budget"})
    await kcv.KingWACodeViolationScraper().scrape("08/01/2026", "09/14/2026")

    assert await _source_alerts(db, since) == []


@pytest.mark.asyncio
async def test_every_failed_source_is_alerted_when_the_whole_scrape_fails(db, monkeypatch, no_backoff):
    since = datetime.now(UTC)
    _connector(monkeypatch, fail={"bellevue", "burien", "accela"}, sdci_fails=True)
    with pytest.raises(RuntimeError, match="every source failed"):
        await kcv.KingWACodeViolationScraper().scrape("08/01/2026", "09/14/2026")

    assert [path for path, _ in await _source_alerts(db, since)] == [
        f"{kcv.SOURCE_FAILURE_ALERT_KIND}:{key}" for key in sorted(
            ("seattle_sdci_code_violations", "bellevue_code_enforcement", "burien_code_enforcement",
             "kingco_accela_code_enforcement"))]


@pytest.mark.asyncio
async def test_the_partial_failure_warning_reaches_the_job_log(
        db, business_user, redis_client, monkeypatch, no_backoff):
    from src.workers.tasks_helpers.enrich import _run_scraper

    job_id = await _job(db, business_user, status="scraping")
    _connector(monkeypatch, fail={"burien"})
    pubsub = redis_client.pubsub()
    pubsub.subscribe(f"job_logs:{job_id}")
    pubsub.get_message(timeout=1)

    recs = await _run_scraper(kcv.KingWACodeViolationScraper, "08/01/2026", "09/14/2026",
                              redis_client, job_id, record_type="code_violation")

    assert {r.enrichment_data["source"] for r in recs} == {"seattle_sdci_code_violations",
                                                           "bellevue_code_enforcement",
                                                           "kingco_accela_code_enforcement"}
    logs = (await db.execute(text("SELECT level, message FROM job_logs WHERE job_id = :j"),
                             {"j": job_id})).all()
    assert [(row.level, row.message) for row in logs] == [(
        "warning",
        "Code violation records from Burien could not be collected this run, so these leads "
        "cover Seattle, Bellevue, and unincorporated King County only. Run this scraper again "
        "later to include Burien.")]
    published = pubsub.get_message(timeout=2)
    assert published and json.loads(published["data"])["level"] == "warning"
    pubsub.close()


@pytest.mark.asyncio
async def test_seattle_requests_require_the_scrape_allowlist(monkeypatch):
    from src.api.middleware.security import validate_scraping_target

    calls: list = []

    def _get(url, **kw):
        calls.append((url, kw))
        return _Resp([SDCI_ROW])

    monkeypatch.setattr(seattle_sdci, "safe_get", _get)
    await seattle_sdci.SeattleSDCISource().fetch("09/01/2026", "09/14/2026")
    assert calls and all(kw.get("require_allowlisted") is True for _, kw in calls)
    validate_scraping_target(calls[0][0], require_allowlisted=True, resolve=False)


def test_stored_upstream_labels_are_collapsed_and_capped():
    long = "OPEN " * 100
    for label in (bellevue._label, burien.label):
        assert label(long) == " ".join(long.split())[:base.LABEL_MAX]
        assert len(label(long)) == base.LABEL_MAX
        assert label("  In   Review ") == "In Review"
        assert label(None) is None and label("   ") is None
        assert label(3389900395) == "3389900395"


def test_burien_case_id_is_kept_only_when_it_is_a_whole_number():
    assert [burien._integral(v) for v in (35072, 35072.0, 35072.5, float("nan"), float("inf"),
                                          "35072", True, None)] == [
        35072, 35072, None, None, None, None, None, None]


@pytest.mark.asyncio
async def test_a_failing_progress_callback_fails_the_scrape_not_the_jurisdiction(monkeypatch):
    monkeypatch.setattr(burien, "_PAGE_SIZE", BUR_PAGED["page_size"])
    monkeypatch.setattr(base, "safe_get", _ArcGIS(burien_pages=BUR_PAGED))
    source = burien.BurienSource()
    scraper = kcv.KingWACodeViolationScraper(sources=[source])

    def _broken(*_a):
        raise ConnectionError("Redis went away")

    scraper.on_progress = _broken
    with pytest.raises(kcv.ProgressCallbackError, match="Redis went away"):
        await scraper.scrape("01/01/2026", "09/14/2026")
    assert scraper.source_status == {} and scraper.scrape_warnings == []
    assert source.on_progress is None


def test_a_connector_without_sources_is_refused():
    with pytest.raises(ValueError, match="at least one source"):
        kcv.KingWACodeViolationScraper(sources=[])


@pytest.mark.asyncio
async def test_a_progress_callback_never_outlives_the_scrape_that_set_it(monkeypatch):
    monkeypatch.setattr(burien, "_PAGE_SIZE", BUR_PAGED["page_size"])
    monkeypatch.setattr(base, "safe_get", _ArcGIS(burien_pages=BUR_PAGED))
    source = burien.BurienSource()
    scraper = kcv.KingWACodeViolationScraper(sources=[source])
    seen: list = []
    scraper.on_progress = lambda *a: seen.append(a)
    await scraper.scrape("01/01/2026", "09/14/2026")
    assert seen and source.on_progress is None
    count = len(seen)

    scraper.on_progress = None
    await scraper.scrape("01/01/2026", "09/14/2026")
    assert source.on_progress is None and scraper.source_status == {source.key: "ok"}
    assert len(seen) == count


def test_run_scraper_still_builds_the_connector_from_its_class():
    # The worker passes record_type only when the constructor takes it; `sources` is not
    # something the worker ever sets.
    scraper = kcv.KingWACodeViolationScraper(record_type="code_violation")
    assert [type(s) for s in scraper.sources] == list(kcv.SOURCES)
    partial = functools.partial(kcv.KingWACodeViolationScraper, sources=[burien.BurienSource()])
    assert [s.key for s in partial().sources] == ["burien_code_enforcement"]


# ── Owner and paid skip trace for parcel-keyed sources ────────────────────────

def _parcel_row(source="bellevue_code_enforcement", parcel_id="2571200050", **over):
    base_row = {"id": "r1", "job_id": "j1", "user_id": "u1", "party_name": None,
                "parcel_id": parcel_id,
                "property_address": "10202 SE 13th Pl, Bellevue WA 98004",
                "mailing_address": "188 BELLEVUE WAY NE UNIT 903, BELLEVUE, WA 98004",
                "property_city": "Bellevue", "property_state": "WA", "property_zip": "98004",
                "enrichment_data": {"source": source, "case_number": "26 116940 EA"}}
    base_row.update(over)
    return SimpleNamespace(**base_row)


def test_owner_lookup_keys_parcel_sources_by_their_printed_pin():
    bel = _parcel_row()
    bur = _parcel_row(source="burien_code_enforcement", parcel_id="7835800148")
    no_pin = _parcel_row(parcel_id=None)
    named = _parcel_row(party_name="SOMEONE")
    # Located-parcel metadata does not make a parcel-keyed source row nameable.
    located_only = _parcel_row(parcel_id=None, enrichment_data={
        "source": "bellevue_code_enforcement", "kc_pin": "2571200050", "kc_pin_status": "matched",
        "kc_pin_source": "king_gis_point_in_parcel", "kc_pin_match": "exact"})
    pin_map = kpl.owner_lookup_pins([bel, bur, no_pin, named, located_only])
    assert pin_map == {"2571200050": [bel], "7835800148": [bur]}

    n = kpl.apply_owner_names(pin_map, {"2571200050": "DVD SE 13TH PL LLC"}, checked_at="t")
    assert n == 1 and bel.party_name == "DVD SE 13TH PL LLC"
    assert bel.enrichment_data["owner_source"] == "king_erealproperty"
    assert bel.enrichment_data["owner_pin"] == "2571200050" == bel.parcel_id
    assert bur.party_name is None


def test_printed_pin_parcels_are_asked_first_because_no_sweep_names_them_later():
    sdci = _parcel_row(parcel_id=None, enrichment_data={
        "source": "seattle_sdci_code_violations", "kc_pin": "9138100481", "kc_pin_status": "matched",
        "kc_pin_source": "king_gis_point_in_parcel", "kc_pin_match": "exact"})
    bel = _parcel_row()
    acc = _parcel_row(source="kingco_accela_code_enforcement", parcel_id="1626069072")
    assert list(kpl.owner_lookup_pins([sdci, bel, acc])) == ["2571200050", "1626069072", "9138100481"]


def test_an_owner_is_not_applied_to_a_row_whose_parcel_changed_since_selection():
    row = _parcel_row()
    pin_map = kpl.owner_lookup_pins([row])
    row.parcel_id = "1111111111"
    assert kpl.apply_owner_names(pin_map, {"2571200050": "DVD SE 13TH PL LLC"}, checked_at="t") == 0
    assert row.party_name is None


def _owned(**ed_over):
    return {"source": "bellevue_code_enforcement", "owner_source": "king_erealproperty",
            "owner_pin": "2571200050", **ed_over}


@pytest.mark.parametrize("source", ["bellevue_code_enforcement", "burien_code_enforcement"])
def test_a_parcel_source_row_is_traced_only_with_the_owner_of_its_own_pin(source):
    ok = _parcel_row(party_name="DVD SE 13TH PL LLC", enrichment_data=_owned(source=source))
    assert build_pending_row_payload(ok) is not None

    for bad in (
        _parcel_row(enrichment_data=_owned(source=source)),  # no owner name
        _parcel_row(party_name="DVD SE 13TH PL LLC",
                    enrichment_data=_owned(source=source, owner_pin="1111111111")),
        _parcel_row(party_name="DVD SE 13TH PL LLC",
                    enrichment_data=_owned(source=source, owner_source="permit_system")),
        _parcel_row(party_name="DVD SE 13TH PL LLC", parcel_id=None,
                    enrichment_data=_owned(source=source)),
        _parcel_row(party_name="DVD SE 13TH PL LLC", parcel_id="257120-0050",
                    enrichment_data=_owned(source=source, owner_pin="257120-0050")),
    ):
        assert build_pending_row_payload(bad) is None


# ── Real enrichment for a stored King code-violation job ──────────────────────

async def _job(db, user, *, status: str) -> str:
    config = ScraperConfig(id=str(uuid.uuid4()), user_id=user.id, name="King CV",
                           county="king", state="WA", record_type="code_violation",
                           fields=["party_name"], enrichment=[], schedule={"frequency": "manual"},
                           deliver={"format": "csv", "emails": []})
    db.add(config)
    await db.commit()
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=config.id, status=status,
               trigger="manual", record_count=0, billed_count=0))
    await db.commit()
    return job_id


@pytest.mark.asyncio
async def test_a_king_cv_job_names_and_mails_parcel_source_rows_without_touching_identity(
    db, business_user, redis_client, tmp_path, monkeypatch,
):
    from tests.test_king_rpacct_mailing import _acct, _extract, _use_extract

    job_id = await _job(db, business_user, status="enriching")
    rows = {}
    for source, pin, address, case in (
        ("bellevue_code_enforcement", "2571200050", "10202 SE 13th Pl, Bellevue WA 98004",
         "26 116940 EA"),
        ("burien_code_enforcement", "7835800148", "13007 12th Ave SW, Burien WA", "CE26-02109"),
    ):
        rid = str(uuid.uuid4())
        dedup = legacy_strong_signature(pin, address)
        db.add(Result(id=rid, user_id=business_user.id, job_id=job_id, party_name=None,
                      parcel_id=pin, property_address=address, legal_description=case,
                      mailing_address=None, dedup_hash=dedup, skip_trace_status="not_attempted",
                      is_duplicate=False, enrichment_data={"source": source, "case_number": case}))
        rows[source] = (rid, pin, dedup)
    # An Accela intake case with no parcel yet but a street and ZIP: never located from its
    # address (the tripwire below), so it keeps no located PIN, owner or mailing.
    no_parcel_id = str(uuid.uuid4())
    db.add(Result(id=no_parcel_id, user_id=business_user.id, job_id=job_id, party_name=None,
                  parcel_id=None, property_address="7016 S LAKERIDGE DR", property_zip="98178",
                  legal_description="ENFR26-0933", mailing_address=None,
                  dedup_hash=uuid.uuid4().hex,
                  skip_trace_status="not_attempted", is_duplicate=False,
                  enrichment_data={"source": "kingco_accela_code_enforcement",
                                   "case_number": "ENFR26-0933"}))
    await db.commit()

    # Real Assessor extract rows for both parcels (snapshot 2026-09-05).
    _use_extract(monkeypatch, _extract(tmp_path, [
        _acct("257120", "0050", "188 BELLEVUE WAY NE UNIT 903", "BELLEVUE  WA", "98004"),
        _acct("783580", "0148", "4801 115TH ST SW", "LAKEWOOD  WA", "98499"),
    ]))
    pages = {"2571200050": ERP_BELLEVUE, "7835800148": ERP_BURIEN}
    erp_calls: list = []

    def _erp(url, *a, **kw):
        erp_calls.append(url)
        return _Resp(text_body=pages[url.split("ParcelNbr=", 1)[1]])

    monkeypatch.setattr(kca, "safe_get", _erp)

    async def _no_wait(_s):
        return None

    monkeypatch.setattr(kca.asyncio, "sleep", _no_wait)
    monkeypatch.setattr("src.scrapers.enrichment.county_gis.batch_enrich_parcels_gis",
                        lambda *a, **kw: {})

    locate_calls: list = []

    def _locate_must_not_run(*a, **kw):
        # Recorded, because enrichment logs and swallows an exception from this step.
        locate_calls.append(a)
        raise AssertionError("parcel-keyed rows are never located from coordinates")

    monkeypatch.setattr(kpl, "resolve_code_violation_mailing", _locate_must_not_run)

    def _go():
        from src.db.session import system_sync_session
        from src.workers.tasks_helpers.enrich import _run_inline_enrichment

        with system_sync_session() as sdb:
            job = sdb.get(Job, job_id)
            config = sdb.get(ScraperConfig, job.scraper_config_id)
            _run_inline_enrichment(sdb, job, redis_client, job_id, config, summary={})

    await asyncio.to_thread(_go)

    got = {str(r.id): r for r in (await db.execute(text(
        "SELECT id, party_name, parcel_id, dedup_hash, mailing_address, enrichment_data "
        "FROM results WHERE job_id = :j"), {"j": job_id})).all()}
    bel_id, bel_pin, bel_dedup = rows["bellevue_code_enforcement"]
    bur_id, bur_pin, bur_dedup = rows["burien_code_enforcement"]
    bel, bur = got[bel_id], got[bur_id]
    assert bel.party_name == "DVD SE 13TH PL LLC"
    assert bur.party_name == "OVERLOOK AT BURIEN LLC"
    for row, pin, dedup in ((bel, bel_pin, bel_dedup), (bur, bur_pin, bur_dedup)):
        assert row.enrichment_data["owner_source"] == "king_erealproperty"
        assert row.enrichment_data["owner_pin"] == pin
        assert (row.parcel_id, row.dedup_hash) == (pin, dedup)
        assert row.enrichment_data["mailing_source"] == "king_rpacct"
        assert "kc_pin_status" not in row.enrichment_data
    assert bel.mailing_address == "188 BELLEVUE WAY NE UNIT 903, BELLEVUE, WA 98004"
    assert bur.mailing_address == "4801 115TH ST SW, LAKEWOOD, WA 98499"
    # Named from the county page for their own PIN, so paid skip trace may take them.
    for row, address in ((bel, "10202 SE 13th Pl, Bellevue WA 98004"),
                         (bur, "13007 12th Ave SW, Burien WA")):
        assert build_pending_row_payload(SimpleNamespace(
            id=row.id, job_id=job_id, user_id=business_user.id, party_name=row.party_name,
            parcel_id=row.parcel_id, property_address=address,
            mailing_address=row.mailing_address, property_city=None, property_state="WA",
            property_zip=None, enrichment_data=row.enrichment_data)) is not None
    assert any(url.endswith("2571200050") for url in erp_calls)
    assert locate_calls == []
    untouched = got[no_parcel_id]
    assert (untouched.party_name, untouched.parcel_id, untouched.mailing_address) == (None, None, None)
    assert untouched.enrichment_data == {"source": "kingco_accela_code_enforcement",
                                         "case_number": "ENFR26-0933"}
