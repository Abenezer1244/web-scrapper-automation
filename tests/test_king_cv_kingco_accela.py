"""Unincorporated King County code enforcement from the county's Accela Citizen Access.

Only the browser is substituted: `FixturePortal` answers the adapter's two portal calls
(a date-range search, a case detail page) with the real pages saved from
aca-prod.accela.com/KINGCO on 2026-09-14 (tests/fixtures/king_cv_accela_*.html, with the
complainant and staff free text replaced by "[redacted]"). Each page is trimmed by deletion
only to the captured elements the adapter reads (the record grid, the no-results message,
the case number, type, status, parcel list and work location) plus their ancestor chain,
as captured; scripts, styles, navigation and ViewState were removed and nothing was added.
Parsing, paging checks, windowing, retries, walls,
budgets, record building and the connector/skip-trace registration run for real.
"""
from __future__ import annotations

import hashlib
from dataclasses import fields
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from src.api.middleware.security import validate_scraping_target
from src.scrapers import king_cv_sources
from src.scrapers import king_wa_code_violation as kcv
from src.scrapers.enrichment import king_parcel_locate as kpl
from src.scrapers.enrichment import skip_trace
from src.scrapers.enrichment.skip_trace import build_pending_row_payload
from src.scrapers.king_cv_sources import kingco_accela as ka

_FIX = Path(__file__).resolve().parent / "fixtures"
_REDACTED = "[redacted]"
_SEARCH = ka.SEARCH_URL
_DETAIL_PREFIX = "https://aca-prod.accela.com/KINGCO/Cap/CapDetail.aspx?Module=Enforce&TabName=Enforce"


def _html(name: str) -> str:
    return (_FIX / f"king_cv_accela_{name}.html").read_text(encoding="utf-8")


PAGE1, PAGE2, PAGE3, LAST = (_html(f"results_34_{n}") for n in ("page1", "page2", "page3", "last"))
PAGE_100PLUS = _html("results_100plus_page1")
PAGE_11_OF_122 = _html("results_122_page11")
NO_RESULTS = _html("no_results")
DETAIL_NO_PARCEL = _html("detail_no_parcel")
DETAIL_1626069072 = _html("detail_1626069072")
DETAIL_3421049053 = _html("detail_3421049053")
SINGLE_RESULT = _html("single_result_detail")
LOGIN = _html("login_page")

# The detail URL the portal redirected the 09/13/2026 one-case search to.
SINGLE_RESULT_URL = f"{_DETAIL_PREFIX}&capID1=26ENF&capID2=00000&capID3=00931&agencyCode=KINGCO&IsToShowInspection="


def _snap(html: str, url: str = _SEARCH) -> ka.PageSnapshot:
    return ka.PageSnapshot(url=url, html=html)


THE_34_PAGES = [_snap(PAGE1), _snap(PAGE2), _snap(PAGE3), _snap(LAST)]
DETAILS_BY_CASE = {"ENFR26-0933": DETAIL_NO_PARCEL, "ENFR26-0938": DETAIL_1626069072,
                   "ENFR26-0936": DETAIL_3421049053, "ENFR26-0931": SINGLE_RESULT}


class FixturePortal:
    """Stands in for the Playwright AccelaPortal, answering from the saved pages.

    ``searches`` maps (start, end) to the page snapshots a search returned; a window
    without an entry gets the portal's real no-results page. ``fail`` holds exceptions
    to raise, in order, before answering a call.
    """

    searches: dict[tuple[date, date], list[ka.PageSnapshot]] = {}
    details: dict[str, str] = DETAILS_BY_CASE
    fail: list[Exception] = []
    calls: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def search(self, start: date, end: date) -> list[ka.PageSnapshot]:
        type(self).calls.append(("search", start, end))
        if type(self).fail:
            raise type(self).fail.pop(0)
        return type(self).searches.get((start, end), [_snap(NO_RESULTS)])

    async def case_detail(self, path: str) -> ka.PageSnapshot:
        url = ka.detail_url(path)
        type(self).calls.append(("detail", path))
        if type(self).fail:
            raise type(self).fail.pop(0)
        cap_id3 = parse_qs(urlsplit(url).query)["capID3"][0]
        case = next(c for c in type(self).details if int(c.rsplit("-", 1)[1]) == int(cap_id3))
        return _snap(type(self).details[case], url)


def use_fixture_portal(monkeypatch, *, searches=None, details=None, fail=()):
    """Point KingCountyAccelaSource at FixturePortal (fresh state) and skip retry sleeps."""
    portal = type("Portal", (FixturePortal,), {
        "searches": dict(searches or {}), "details": dict(details or DETAILS_BY_CASE),
        "fail": list(fail), "calls": []})
    monkeypatch.setattr(ka.KingCountyAccelaSource, "portal_class", portal)

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(ka.asyncio, "sleep", _no_sleep)
    return portal


def _hash(case: str) -> str:
    return hashlib.sha256(f"kingco_accela_code_enforcement|{case}".encode()).hexdigest()[:32]


# ── Windows ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("start", "end", "windows"), [
    (date(2026, 9, 13), date(2026, 9, 13), [(date(2026, 9, 13), date(2026, 9, 13))]),
    (date(2026, 9, 7), date(2026, 9, 13), [(date(2026, 9, 7), date(2026, 9, 13))]),
    (date(2026, 9, 7), date(2026, 9, 14), [(date(2026, 9, 7), date(2026, 9, 13)),
                                          (date(2026, 9, 14), date(2026, 9, 14))]),
    (date(2026, 8, 28), date(2026, 9, 14), [(date(2026, 8, 28), date(2026, 9, 3)),
                                           (date(2026, 9, 4), date(2026, 9, 10)),
                                           (date(2026, 9, 11), date(2026, 9, 14))]),
    (date(2026, 9, 14), date(2026, 9, 13), []),
])
def test_windows_are_consecutive_inclusive_and_at_most_seven_days(start, end, windows):
    got = ka.split_windows(start, end)
    assert got == windows
    for (a, b), nxt in zip(got, got[1:], strict=False):
        assert (b - a).days <= 6 and (nxt[0] - b).days == 1


def test_one_day_windows_cover_every_day():
    days = ka.split_windows(date(2026, 9, 12), date(2026, 9, 14), days=1)
    assert days == [(date(2026, 9, d), date(2026, 9, d)) for d in (12, 13, 14)]


# ── Results pages ────────────────────────────────────────────────────────────

def test_a_results_page_parses_every_row_without_its_description():
    page = ka.parse_results_page(PAGE1)
    assert page.showing == (1, 10, 34, False) and page.has_next
    assert len(page.rows) == 10
    first = page.rows[0]
    assert first == ka.GridRow(
        opened=date(2026, 9, 14), case_number="ENFR26-0933",
        detail_path="/KINGCO/Cap/CapDetail.aspx?Module=Enforce&TabName=Enforce&capID1=26ENF"
                    "&capID2=00000&capID3=00933&agencyCode=KINGCO&IsToShowInspection=",
        record_type="Code Enforcement Case", status="Intake Processing",
        address="7016 S LAKERIDGE DR", zip="98178")
    # The grid row type has no field for the complainant's text or the staff notes.
    assert {f.name for f in fields(ka.GridRow)} == {
        "opened", "case_number", "detail_path", "record_type", "status", "address", "zip"}
    assert not any(_REDACTED in str(v) for r in page.rows for v in vars(r).values())


def test_paging_is_detected_from_the_pager_and_the_showing_range():
    assert [ka.parse_results_page(h).showing for h in (PAGE2, PAGE3, LAST)] == [
        (11, 20, 34, False), (21, 30, 34, False), (31, 34, 34, False)]
    assert [ka.parse_results_page(h).has_next for h in (PAGE2, PAGE3, LAST)] == [True, True, False]
    assert len(ka.parse_results_page(LAST).rows) == 4
    # "100+" is a display cap, not the end: the portal pages on to "101-110 of 122".
    assert ka.parse_results_page(PAGE_100PLUS).showing == (1, 10, 100, True)
    assert ka.parse_results_page(PAGE_100PLUS).has_next
    assert ka.parse_results_page(PAGE_11_OF_122).showing == (101, 110, 122, False)
    assert ka.parse_results_page(PAGE_11_OF_122).has_next


def test_no_results_is_an_empty_page_but_a_page_without_grid_or_message_is_a_format_break():
    assert ka.parse_results_page(NO_RESULTS) == ka.ResultsPage(rows=[], showing=None, has_next=False)
    with pytest.raises(ka.AccelaFormatError, match="neither the record grid"):
        ka.parse_results_page(LOGIN)


def test_canary_a_renamed_grid_column_or_unreadable_rows_fail_loud():
    with pytest.raises(ka.AccelaFormatError, match="headers changed"):
        ka.parse_results_page(PAGE1.replace(">Record Number<", ">Case Number<"))
    with pytest.raises(ka.AccelaFormatError, match="none had a case number and date"):
        ka.parse_results_page(PAGE1.replace("09/14/2026<", "Sep 14 2026<").replace("09/13/2026<", "Sep 13<")
                              .replace("09/12/2026<", "Sep 12<").replace("09/11/2026<", "Sep 11<")
                              .replace("09/10/2026<", "Sep 10<"))


@pytest.mark.parametrize(("raw", "parts"), [
    ("7016 S LAKERIDGE DR, 98178", ("7016 S LAKERIDGE DR", "98178")),
    ("4407 332ND AVE SE, WA 98024", ("4407 332ND AVE SE", "98024")),
    ("30028 SE LAKE RETREAT S DR, 98051 United States", ("30028 SE LAKE RETREAT S DR", "98051")),
    ("21617 NE 159TH ST, 98077-1234", ("21617 NE 159TH ST", "98077")),
    ("13007 12th Ave SW", ("13007 12th Ave SW", None)),
    ("United States", (None, None)),
    (", 98178", (None, None)),
    ("", (None, None)),
    (None, (None, None)),
])
def test_address_shapes(raw, parts):
    assert ka.split_address(raw) == parts


def test_the_street_and_zip_are_stored_apart_so_skip_trace_never_reads_the_zip_as_street():
    # The shared skip-trace parser reads a comma-less "STREET ZIP" line as all street:
    # this is why the adapter stores the ZIP in the record's property_zip instead.
    assert skip_trace._parse_full_address("21617 NE 159TH ST 98077")["street"] == "21617 NE 159TH ST 98077"

    row = next(r for r in ka.parse_results_page(PAGE1).rows if r.case_number == "ENFR26-0938")
    record = ka.build_record(row, ka.parse_case_detail(DETAIL_1626069072, "ENFR26-0938"))
    assert (record.property_address, record.property_zip) == ("21617 NE 159TH ST", "98077")
    # property_zip stays out of to_dict(), which other scrapers hash into their identity.
    assert "property_zip" not in record.to_dict()

    # After parcel enrichment names the owner and the city, the traced street is the street.
    owned = {"source": "kingco_accela_code_enforcement", "owner_source": "king_erealproperty",
             "owner_pin": "1626069072"}
    payload = build_pending_row_payload(_accela_row(
        party_name="OWNER LLC", property_address=record.property_address, property_city="WOODINVILLE",
        property_zip=record.property_zip, enrichment_data=owned))
    assert (payload["property_address"], payload["city"], payload["state"], payload["zip"]) == (
        "21617 NE 159TH ST", "WOODINVILLE", "WA", "98077")


def test_one_searchs_pages_give_the_windows_rows_once_each():
    rows = ka.rows_from_search(THE_34_PAGES, date(2026, 9, 7), date(2026, 9, 13))
    every = [r for h in (PAGE1, PAGE2, PAGE3, LAST) for r in ka.parse_results_page(h).rows]
    assert len(every) == 34
    assert len(rows) == len([r for r in every if r.opened <= date(2026, 9, 13)]) == 27
    assert len({r.case_number for r in rows}) == 27
    assert min(r.opened for r in rows) == date(2026, 9, 7) and max(r.opened for r in rows) == date(2026, 9, 13)


def test_a_page_that_did_not_advance_or_a_search_not_read_to_the_end_fails():
    with pytest.raises(ka.AccelaFormatError, match="paging did not advance"):
        ka.rows_from_search([_snap(PAGE1), _snap(PAGE1)], date(2026, 9, 7), date(2026, 9, 14))
    with pytest.raises(ka.AccelaFormatError, match="not read to the end"):
        ka.rows_from_search([_snap(PAGE1), _snap(PAGE2)], date(2026, 9, 7), date(2026, 9, 14))
    with pytest.raises(ka.AccelaFormatError, match="not read to the end"):
        ka.rows_from_search([_snap(PAGE_100PLUS)], date(2026, 8, 15), date(2026, 9, 14))
    assert ka.rows_from_search([_snap(NO_RESULTS)], date(2030, 1, 1), date(2030, 1, 2)) == []


def test_a_one_case_search_lands_on_the_detail_page():
    snap = _snap(SINGLE_RESULT, SINGLE_RESULT_URL)
    assert ka.is_detail_page(snap) and not ka.is_detail_page(_snap(PAGE1))
    rows = ka.rows_from_search([snap], date(2026, 9, 13), date(2026, 9, 13))
    assert rows == [ka.GridRow(
        opened=date(2026, 9, 13), case_number="ENFR26-0931",
        detail_path="/KINGCO/Cap/CapDetail.aspx?Module=Enforce&TabName=Enforce&capID1=26ENF"
                    "&capID2=00000&capID3=00931&agencyCode=KINGCO&IsToShowInspection=",
        record_type="Code Enforcement Case", status="No Further Action Required",
        address="8506 S 116TH ST", zip="98178")]
    # The detail page has no opened date, so a longer window cannot date the case.
    assert ka.rows_from_search([snap], date(2026, 9, 12), date(2026, 9, 13)) is None


# ── Detail pages and records ─────────────────────────────────────────────────

def test_detail_pages_give_the_parcel_when_there_is_one():
    assert ka.parse_case_detail(DETAIL_1626069072, "ENFR26-0938") == ka.CaseDetail(
        case_number="ENFR26-0938", parcel_numbers=["1626069072"], address="21617 NE 159TH ST",
        zip="98077")
    assert ka.parse_case_detail(DETAIL_3421049053, "ENFR26-0936").parcel_numbers == ["3421049053"]
    # Intake cases often have no parcel or work location yet.
    assert ka.parse_case_detail(DETAIL_NO_PARCEL, "ENFR26-0933") == ka.CaseDetail(
        case_number="ENFR26-0933", parcel_numbers=[], address=None, zip=None)
    with pytest.raises(ka.AccelaFormatError, match="expected 'ENFR26-0933'"):
        ka.parse_case_detail(DETAIL_1626069072, "ENFR26-0933")


def test_records_carry_the_case_identity_and_the_printed_pin_and_no_free_text():
    rows = {r.case_number: r for r in ka.parse_results_page(PAGE1).rows}
    with_pin = ka.build_record(rows["ENFR26-0938"], ka.parse_case_detail(DETAIL_1626069072, "ENFR26-0938"))
    assert with_pin.parcel_id == "1626069072"
    assert with_pin.raw_html_hash == _hash("ENFR26-0938")
    assert (with_pin.date_recorded, with_pin.legal_description, with_pin.party_name) == (
        "09/14/2026", "ENFR26-0938", None)
    assert (with_pin.property_address, with_pin.property_zip) == ("21617 NE 159TH ST", "98077")
    assert with_pin.enrichment_data == {
        "source": "kingco_accela_code_enforcement", "case_number": "ENFR26-0938",
        "status": "Intake Processing", "violation_category": None,
        "case_type": "Code Enforcement Case", "opened_date": "09/14/2026",
        "source_parcel_numbers": ["1626069072"]}

    no_pin = ka.build_record(rows["ENFR26-0933"], ka.parse_case_detail(DETAIL_NO_PARCEL, "ENFR26-0933"))
    assert no_pin.parcel_id is None and no_pin.enrichment_data["source_parcel_numbers"] == []
    assert (no_pin.property_address, no_pin.property_zip) == ("7016 S LAKERIDGE DR", "98178")

    for record in (with_pin, no_pin):
        # The fixtures' description and notes cells read "[redacted]": a record that
        # carried either would carry that marker.
        assert _REDACTED not in str(record.to_dict())
        assert not any("desc" in k or "note" in k for k in record.enrichment_data)


def test_a_case_on_several_parcels_keeps_them_all_but_names_no_single_parcel():
    row = ka.parse_results_page(PAGE1).rows[1]
    detail = ka.CaseDetail(case_number=row.case_number, parcel_numbers=["1626069072", "0121029085"],
                           address=None, zip=None)
    record = ka.build_record(row, detail)
    assert record.parcel_id is None
    assert record.enrichment_data["source_parcel_numbers"] == ["1626069072", "0121029085"]


def test_parcel_numbers_are_normalized_like_every_king_source():
    dashed = DETAIL_1626069072.replace("Parcel Number:1626069072", "Parcel Number:162606-9072")
    assert ka.parse_case_detail(dashed, "ENFR26-0938").parcel_numbers == ["1626069072"]
    short = DETAIL_1626069072.replace("Parcel Number:1626069072", "Parcel Number:162606907")
    assert ka.parse_case_detail(short, "ENFR26-0938").parcel_numbers == []


def test_detail_links_must_stay_on_the_portal():
    assert ka.detail_url(ka.parse_results_page(PAGE1).rows[0].detail_path).startswith(_DETAIL_PREFIX)
    for bad in ("https://evil.example/KINGCO/Cap/CapDetail.aspx?x=1", "//evil.example/KINGCO/Cap/CapDetail.aspx",
                "/KINGCO/Login.aspx", "http://aca-prod.accela.com/KINGCO/Cap/CapDetail.aspx?x=1"):
        with pytest.raises(ka.AccelaFormatError, match="unexpected case detail link"):
            ka.detail_url(bad)


# ── Walls ────────────────────────────────────────────────────────────────────

def test_the_login_page_is_a_wall_and_the_normal_pages_are_not():
    assert ka.detect_wall(_snap(LOGIN, "https://aca-prod.accela.com/KINGCO/Login.aspx")) == "login page"
    for html in (PAGE1, PAGE_100PLUS, NO_RESULTS, DETAIL_NO_PARCEL, DETAIL_1626069072):
        assert ka.detect_wall(_snap(html)) is None
    assert ka.detect_wall(_snap(SINGLE_RESULT, SINGLE_RESULT_URL)) is None
    with pytest.raises(ka.AccelaAccessWallError, match="login page"):
        ka.ensure_no_wall(_snap(LOGIN, "https://aca-prod.accela.com/KINGCO/Login.aspx?ReturnUrl=x"))


def test_a_captcha_or_terms_acceptance_control_is_a_wall():
    captcha = NO_RESULTS.replace("</form>", '<div class="g-recaptcha" data-sitekey="k"></div></form>', 1)
    assert ka.detect_wall(_snap(captcha)) == "captcha"
    terms = NO_RESULTS.replace(
        "</form>", '<input type="checkbox" id="ctl00_PlaceHolderMain_chkAcceptTerms"/></form>', 1)
    assert ka.detect_wall(_snap(terms)) == "terms acceptance"


# ── The source end to end (portal answers substituted) ───────────────────────

async def test_a_one_day_fetch_ships_the_case_with_its_parcel(monkeypatch):
    day = date(2026, 9, 13)
    portal = use_fixture_portal(monkeypatch, searches={(day, day): [_snap(SINGLE_RESULT, SINGLE_RESULT_URL)]})
    recs = await ka.KingCountyAccelaSource().fetch("09/13/2026", "09/13/2026")
    assert [(r.legal_description, r.parcel_id, r.date_recorded, r.raw_html_hash) for r in recs] == [
        ("ENFR26-0931", "1180001661", "09/13/2026", _hash("ENFR26-0931"))]
    assert [c[0] for c in portal.calls] == ["search", "detail"]


async def test_a_multi_day_window_that_lands_on_one_case_is_searched_day_by_day(monkeypatch):
    # Real pages; which window answered with which page is assigned for this test.
    d12, d13 = date(2026, 9, 12), date(2026, 9, 13)
    portal = use_fixture_portal(monkeypatch, searches={
        (d12, d13): [_snap(SINGLE_RESULT, SINGLE_RESULT_URL)],
        (d13, d13): [_snap(SINGLE_RESULT, SINGLE_RESULT_URL)]})
    recs = await ka.KingCountyAccelaSource().fetch("09/12/2026", "09/13/2026")
    assert [r.legal_description for r in recs] == ["ENFR26-0931"]
    assert recs[0].date_recorded == "09/13/2026"
    assert portal.calls[:3] == [("search", d12, d13), ("search", d12, d12), ("search", d13, d13)]


async def test_a_wall_stops_the_source_without_a_retry(monkeypatch):
    portal = use_fixture_portal(monkeypatch, searches={
        (date(2026, 9, 7), date(2026, 9, 13)): [_snap(LOGIN, "https://aca-prod.accela.com/KINGCO/Login.aspx")]})
    with pytest.raises(ka.AccelaAccessWallError):
        await ka.KingCountyAccelaSource().fetch("09/07/2026", "09/13/2026")
    assert portal.calls == [("search", date(2026, 9, 7), date(2026, 9, 13))]


async def test_a_transient_failure_is_retried_and_a_persistent_one_fails_the_source(monkeypatch):
    day = date(2026, 9, 13)
    searches = {(day, day): [_snap(SINGLE_RESULT, SINGLE_RESULT_URL)]}
    portal = use_fixture_portal(monkeypatch, searches=searches,
                                fail=[RuntimeError("the portal returned its error page")])
    recs = await ka.KingCountyAccelaSource().fetch("09/13/2026", "09/13/2026")
    assert len(recs) == 1 and [c[0] for c in portal.calls] == ["search", "search", "detail"]

    from src.config import settings
    portal = use_fixture_portal(monkeypatch, searches=searches, fail=[
        TimeoutError("Timeout 30000ms exceeded")] * settings.MAX_RETRIES)
    with pytest.raises(RuntimeError, match="failed after"):
        await ka.KingCountyAccelaSource().fetch("09/13/2026", "09/13/2026")
    assert len(portal.calls) == settings.MAX_RETRIES


async def test_more_cases_than_one_run_can_look_up_fails_before_any_detail_page(monkeypatch):
    monkeypatch.setattr(ka, "MAX_DETAIL_PAGES", 20)
    portal = use_fixture_portal(monkeypatch, searches={(date(2026, 9, 7), date(2026, 9, 13)): THE_34_PAGES})
    with pytest.raises(ka.AccelaBudgetError, match="more than 20 cases"):
        await ka.KingCountyAccelaSource().fetch("09/07/2026", "09/13/2026")
    assert all(c[0] == "search" for c in portal.calls)


async def test_the_time_budget_stops_the_source(monkeypatch):
    monkeypatch.setattr(ka, "TIME_BUDGET_SECONDS", 0)
    portal = use_fixture_portal(monkeypatch)
    with pytest.raises(ka.AccelaBudgetError, match="time budget"):
        await ka.KingCountyAccelaSource().fetch("09/07/2026", "09/13/2026")
    assert portal.calls == []


# ── Registration, owner and paid skip trace ──────────────────────────────────

def test_the_source_is_registered_everywhere_a_king_source_must_be():
    assert ka.KingCountyAccelaSource in kcv.SOURCES
    assert kcv.SOURCES[-1] is ka.KingCountyAccelaSource  # slowest source runs last
    assert king_cv_sources.KINGCO_ACCELA == "kingco_accela_code_enforcement"
    assert king_cv_sources.KINGCO_ACCELA in king_cv_sources.PARCEL_AT_SCRAPE_SOURCES
    assert king_cv_sources.KINGCO_ACCELA in skip_trace._KING_CODE_VIOLATION_SOURCES
    assert king_cv_sources.KINGCO_ACCELA in skip_trace.CODE_VIOLATION_SOURCES
    validate_scraping_target(_SEARCH, require_allowlisted=True, resolve=False)
    assert kcv.KingWACodeViolationScraper.collection_scope("code_violation").note == (
        "Collected from the code enforcement records of Seattle, Bellevue, Burien, "
        "and unincorporated King County.")


def _accela_row(**over):
    row = {"id": "r1", "job_id": "j1", "user_id": "u1", "party_name": None,
           "parcel_id": "1626069072", "property_address": "21617 NE 159TH ST",
           "mailing_address": "21617 NE 159TH ST, WOODINVILLE, WA 98077",
           "property_city": None, "property_state": "WA", "property_zip": "98077",
           "enrichment_data": {"source": "kingco_accela_code_enforcement", "case_number": "ENFR26-0938"}}
    row.update(over)
    return SimpleNamespace(**row)


def test_owner_is_looked_up_for_the_printed_pin_and_traced_only_for_that_pin():
    row, no_pin = _accela_row(), _accela_row(parcel_id=None)
    assert kpl.owner_lookup_pins([row, no_pin]) == {"1626069072": [row]}

    owned = {"source": "kingco_accela_code_enforcement", "owner_source": "king_erealproperty",
             "owner_pin": "1626069072"}
    assert build_pending_row_payload(_accela_row(party_name="OWNER LLC", enrichment_data=owned)) is not None
    assert build_pending_row_payload(_accela_row(party_name="OWNER LLC", enrichment_data={
        **owned, "owner_pin": "3421049053"})) is None
    assert build_pending_row_payload(_accela_row(enrichment_data=owned)) is None
    assert build_pending_row_payload(_accela_row(party_name="OWNER LLC", parcel_id=None,
                                                 enrichment_data=owned)) is None
