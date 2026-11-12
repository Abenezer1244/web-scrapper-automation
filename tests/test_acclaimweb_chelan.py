"""AcclaimWeb reads every row of a search, from the responses that search caused.

Fixtures: Chelan's real GridResults pages for 10/07/2026 (43 rows, 4 pages of 11,
captured 2026-11-12; person names replaced by PARTY<n>). A real Chromium page runs a
portal page whose Search button loads the grid shell (PartialGrid) and the first rows
page (GridResults), the way Chelan and Douglas do; the portal's responses come from the
fixtures. Regressions covered (verified live 2026-11-12): the grid was read before it
refreshed (rows of the previous search), only the first page was ever read, and a
partial read still counted as success.
"""
import asyncio
import copy
import json
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from src.scrapers.reliability import ScraperExecutionError, TransientScrapeError
from src.scrapers.templates.acclaimweb import (
    _DOC_TYPE_MAP,
    AcclaimWebScraper,
    ChelanAcclaimWebScraper,
    _doc_type_matches,
    _grid_date,
    _grid_item,
)

_FIX = Path(__file__).parent / "fixtures" / "acclaimweb_chelan"
_PAGES = [json.loads((_FIX / f"gridresults_20261007_p{n}.json").read_text()) for n in range(1, 5)]
_HOST = "https://acclaim.fixture.test"
_SEARCH = f"{_HOST}/AcclaimWeb/search/SearchTypeRecordDate"

# The portal's own sequence: shell first; a shell with rows binds the grid to
# GridResults and the grid then posts page 1; an empty search's shell has no binding.
_HTML = """<html><body><div id="grid"></div>
<button id="SearchBtn" onclick="go()">Search</button>
<script>
async function go(tag) {
  const shell = await fetch('/AcclaimWeb/Search/PartialGrid' + (tag ? '?' + tag : ''));
  const t = await shell.text();
  if (!t.includes('GridResults')) { document.getElementById('grid').innerText = 'No Results to Display'; return; }
  if (__GRID_NEVER__) return;
  await fetch('/AcclaimWeb/Search/GridResults', {method: 'POST', body: '__FIRST_BODY__' + (tag ? '&' + tag : ''),
      headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}});
}
</script></body></html>"""
_SHELL_ROWS = "<div class='t-grid'></div><script>ajax: '/AcclaimWeb/Search/GridResults'</script>"
_SHELL_EMPTY = "var rowCount =0; &nbsp; Select Columns: Close Done"


async def _portal(browser, pages, *, empty_text="No Results to Display", first_body="page=1&size=11",
                  shell_status=200, first_status=200, grid_never=False, hold=None):
    """A page on the fixture portal. ``pages`` is the GridResults page list (None = no rows).

    ``first_body`` is what the grid posts first: Chelan's paged form, or Douglas's
    unpaged ``sort=&group=&filter=`` (answered with every row at once). ``hold``: an
    asyncio.Event; GridResults requests tagged ``old`` wait for it, and are answered
    with ``hold_pages`` (a late answer to an earlier search).
    """
    page = await browser.new_page()

    async def serve(route):
        req = route.request
        url = req.url.split("?")[0]
        if url.endswith("/Search/PartialGrid"):
            body = _SHELL_EMPTY if pages is None else _SHELL_ROWS
            await route.fulfill(status=shell_status, content_type="text/html", body=body)
        elif url.endswith("/Search/GridResults"):
            form = parse_qs(req.post_data or "")
            n = int(form.get("page", ["1"])[0])
            if "old" in form and hold is not None:
                await hold["event"].wait()
                await route.fulfill(status=200, content_type="application/json",
                                    body=json.dumps(hold["pages"][0]))
                return
            if hold is not None and n == 1:
                # This search's own page 1: the earlier search's answer lands first.
                hold["event"].set()
                await asyncio.sleep(0.3)
            if pages is None or n > len(pages) or pages[n - 1] is None:
                await route.fulfill(status=500, body="error")
            else:
                await route.fulfill(status=first_status if n == 1 else 200,
                                    content_type="application/json", body=json.dumps(pages[n - 1]))
        else:
            html = (_HTML.replace("__FIRST_BODY__", first_body)
                    .replace("__GRID_NEVER__", "true" if grid_never else "false"))
            if not empty_text:
                html = html.replace("No Results to Display", "")
            await route.fulfill(status=200, content_type="text/html", body=html)

    await page.route(f"{_HOST}/**", serve)
    await page.goto(_SEARCH)
    return page


def _scraper(page, cls=ChelanAcclaimWebScraper, record_type="probate"):
    s = cls(f"{_HOST}/AcclaimWeb", "chelan", "wa", record_types=[record_type], record_type=record_type)
    s.page = page
    return s


@pytest.fixture
async def browser():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        b = await p.chromium.launch()
        try:
            yield b
        finally:
            await b.close()


async def test_every_page_of_the_search_is_read(browser):
    s = _scraper(await _portal(browser, _PAGES))
    rows = await s._read_grid_rows(*await _first_response(s), "10/07/2026", "10/07/2026")
    assert len(rows) == 43 == _PAGES[0]["total"]
    assert len({r["TransactionItemId"] for r in rows}) == 43


async def test_search_returns_only_the_requested_type_from_all_pages(browser):
    s = _scraper(await _portal(browser, _PAGES), record_type="probate")
    recs = await s._search_and_read("10/07/2026", "10/07/2026")
    # Every TOD across the four pages (Chelan probate = DC, TOD); AFFD is not probate.
    expected = sum(1 for p in _PAGES for r in p["data"] if r["DocType"] in ("DC", "TOD"))
    assert expected >= 1
    assert len(recs) == expected
    assert {r.doc_type for r in recs} <= {"DC", "TOD"}
    assert all(r.date_recorded == "10/07/2026" for r in recs)


async def test_a_day_with_no_recordings_is_a_verified_zero(browser):
    s = _scraper(await _portal(browser, None))
    assert await s._search_and_read("10/04/2026", "10/04/2026") == []


async def test_no_rows_and_no_empty_marker_fails_instead_of_reporting_zero(browser):
    s = _scraper(await _portal(browser, None, empty_text=""))
    with pytest.raises(ScraperExecutionError):
        await s._search_and_read("10/04/2026", "10/04/2026")


async def test_a_page_that_cannot_be_read_fails_the_search(browser):
    pages = [_PAGES[0], _PAGES[1], None, _PAGES[3]]
    s = _scraper(await _portal(browser, pages))
    with pytest.raises(TransientScrapeError, match="grid page 3"):
        await s._search_and_read("10/07/2026", "10/07/2026")


async def test_rows_short_of_the_portal_total_fail_the_search(browser):
    pages = copy.deepcopy(_PAGES)
    pages[3]["data"] = pages[3]["data"][:-1]  # the last page lost a row
    for p in pages:
        p["total"] = 43
    pages.append({"total": 43, "data": []})  # and nothing more comes
    s = _scraper(await _portal(browser, pages))
    with pytest.raises(ScraperExecutionError):
        await s._search_and_read("10/07/2026", "10/07/2026")


async def test_rows_from_another_day_are_stale_and_fail_the_search(browser):
    # What the old DOM read returned: the grid of the previous search.
    s = _scraper(await _portal(browser, _PAGES))
    with pytest.raises(ScraperExecutionError, match="outside the searched days"):
        await s._search_and_read("10/08/2026", "10/08/2026")


async def test_a_row_missing_its_instrument_number_is_schema_drift(browser):
    pages = copy.deepcopy(_PAGES)
    pages[1]["data"][0]["InstrumentNumber"] = None
    s = _scraper(await _portal(browser, pages))
    with pytest.raises(ScraperExecutionError, match="schema drift"):
        await s._search_and_read("10/07/2026", "10/07/2026")


# ── Codex 2a review: correlation, explicit zero, transient vs permanent ──────

async def test_a_late_answer_to_an_earlier_search_is_never_read(browser):
    # An earlier search's page-1 request is still in flight (held) when we click; it
    # answers with one same-day DT row (total 1) right after our click. Read as ours,
    # it would turn this day's TOD leads into none. The fixture answers it first.
    late = copy.deepcopy(_PAGES[0])
    late["data"], late["total"] = [r for r in late["data"] if r["DocType"] == "DT"][:1], 1
    hold = {"event": asyncio.Event(), "pages": [late]}
    page = await _portal(browser, _PAGES, hold=hold)
    await page.evaluate("() => { go('old=1'); }")  # not awaited: its grid request hangs
    await page.wait_for_timeout(500)
    s = _scraper(page)
    recs = await s._search_and_read("10/07/2026", "10/07/2026")
    expected = sum(1 for p in _PAGES for r in p["data"] if r["DocType"] in ("DC", "TOD"))
    assert expected >= 1 and len(recs) == expected


async def test_a_failed_grid_shell_is_transient_not_empty(browser):
    s = _scraper(await _portal(browser, None, shell_status=500))
    with pytest.raises(TransientScrapeError):
        await s._search_and_read("10/04/2026", "10/04/2026")


async def test_a_shell_with_rows_whose_grid_never_loads_is_not_empty(browser):
    s = _scraper(await _portal(browser, _PAGES, grid_never=True))
    with pytest.raises(TransientScrapeError, match="never loaded"):
        await s._search_and_read("10/07/2026", "10/07/2026")


async def test_an_http_error_first_grid_page_is_not_a_zero(browser):
    zero = {"data": [], "total": 0}
    s = _scraper(await _portal(browser, [zero], first_status=500))
    with pytest.raises(TransientScrapeError, match="HTTP 500"):
        await s._search_and_read("10/07/2026", "10/07/2026")


async def test_a_failed_later_page_is_transient(browser):
    s = _scraper(await _portal(browser, [_PAGES[0], _PAGES[1], None, _PAGES[3]]))
    with pytest.raises(TransientScrapeError, match="grid page 3"):
        await s._search_and_read("10/07/2026", "10/07/2026")


async def test_count_and_schema_failures_are_permanent_not_transient(browser):
    pages = copy.deepcopy(_PAGES)
    pages[1]["data"][0]["InstrumentNumber"] = None
    s = _scraper(await _portal(browser, pages))
    with pytest.raises(ScraperExecutionError) as err:
        await s._search_and_read("10/07/2026", "10/07/2026")
    assert not isinstance(err.value, TransientScrapeError)


async def test_a_bound_grid_that_reports_zero_rows_is_not_a_verified_zero(browser):
    # The shell bound a grid (rows exist) but page 1 says total 0: contradictory.
    s = _scraper(await _portal(browser, [{"data": [], "total": 0}]))
    with pytest.raises(ScraperExecutionError, match="bound but reported no rows") as err:
        await s._search_and_read("10/07/2026", "10/07/2026")
    assert not isinstance(err.value, TransientScrapeError)


@pytest.mark.parametrize("bad", [{"RecordDate": "2026/13/45"}, {"RecordDate": "/Date(99999999999999999)/"}])
async def test_an_impossible_record_date_is_schema_drift(browser, bad):
    pages = copy.deepcopy(_PAGES)
    pages[2]["data"][0].update(bad)
    s = _scraper(await _portal(browser, pages))
    with pytest.raises(ScraperExecutionError, match="schema drift"):
        await s._search_and_read("10/07/2026", "10/07/2026")


async def test_a_row_that_is_not_an_object_is_schema_drift(browser):
    pages = copy.deepcopy(_PAGES)
    pages[3]["data"][0] = "not a row"
    s = _scraper(await _portal(browser, pages))
    with pytest.raises(ScraperExecutionError, match="schema drift"):
        await s._search_and_read("10/07/2026", "10/07/2026")


async def test_an_aborted_later_page_request_is_transient(browser):
    page = await _portal(browser, _PAGES)

    async def abort_page_2(route):
        if "page=2" in (route.request.post_data or ""):
            await route.abort("connectionreset")
        else:
            await route.fallback()

    await page.route(f"{_HOST}/AcclaimWeb/Search/GridResults", abort_page_2)
    s = _scraper(page)
    with pytest.raises(TransientScrapeError, match="grid page 2 request failed"):
        await s._search_and_read("10/07/2026", "10/07/2026")


# ── Douglas's dialect: {"Data", "Total"}, every row in one unpaged response ─────

_DOUGLAS = json.loads((_FIX / "douglas_gridresults_20261006.json").read_text())


async def test_douglas_unpaged_grid_is_read_whole(browser):
    page = await _portal(browser, [_DOUGLAS], first_body="sort=&group=&filter=")
    s = _scraper(page, cls=AcclaimWebScraper, record_type="pre_foreclosure")
    s.county = "douglas"
    recs = await s._search_and_read("10/06/2026", "10/06/2026")
    lis = [r for r in _DOUGLAS["Data"] if r["DocTypeDescription"] == "LIS PENDENS"]
    assert lis, "fixture control: the day has a lis pendens"
    assert len(recs) == len(lis)
    assert recs[0].doc_type == "LIS PENDENS"
    assert recs[0].date_recorded == "10/06/2026"
    # Douglas leads stay keyed without a parcel (see _grid_item).
    assert recs[0].parcel_id in (None, "")


async def test_douglas_unpaged_grid_short_of_its_total_fails(browser):
    short = copy.deepcopy(_DOUGLAS)
    short["Data"] = short["Data"][:-1]
    page = await _portal(browser, [short], first_body="sort=&group=&filter=")
    s = _scraper(page, cls=AcclaimWebScraper, record_type="pre_foreclosure")
    with pytest.raises(ScraperExecutionError, match="not paged"):
        await s._search_and_read("10/06/2026", "10/06/2026")


def test_douglas_dates_and_doc_type_text_are_read():
    item = _grid_item(_DOUGLAS["Data"][0])
    assert item["date_recorded"] == "10/06/2026"
    assert item["doc_type"] == _DOUGLAS["Data"][0]["DocTypeDescription"]
    assert item["parcel"] == ""


async def _first_response(s):
    async with s.page.expect_response(lambda r: r.url.endswith("/Search/GridResults")) as ri:
        await s._submit_search()
    return (await ri.value,)


# ── Chelan's own document codes ──────────────────────────────────────────────

def _kw(cls, rt):
    return cls.DOC_TYPE_MAP[rt]


def test_chelan_nod_is_a_discontinuance_not_a_pre_foreclosure():
    assert not _doc_type_matches("NOD", _kw(ChelanAcclaimWebScraper, "pre_foreclosure"))
    # CONTROL: the shared map (Douglas) still reads NOD as Notice of Default.
    assert _doc_type_matches("NOD", _kw(AcclaimWebScraper, "pre_foreclosure"))


@pytest.mark.parametrize("code", ["NTS", "NTSCOM", "LP"])
def test_chelan_pre_foreclosure_codes(code):
    assert _doc_type_matches(code, _kw(ChelanAcclaimWebScraper, "pre_foreclosure"))


def test_chelan_affidavit_is_not_probate_but_death_certificate_is():
    kw = _kw(ChelanAcclaimWebScraper, "probate")
    assert not _doc_type_matches("AFFD", kw)
    assert _doc_type_matches("DC", kw)
    assert _doc_type_matches("TOD", kw)


def test_chelan_death_certificate_code_is_a_death_and_tod_stays_a_living_owner():
    from src.scrapers.probate import ProbateSignal, classify_probate_signal

    assert classify_probate_signal("DC") is ProbateSignal.DEATH_INHERITANCE
    assert classify_probate_signal("TOD") is ProbateSignal.TOD_LIVING_OWNER


def test_douglas_vocabulary_is_unchanged():
    assert AcclaimWebScraper.DOC_TYPE_MAP is _DOC_TYPE_MAP


def test_registry_routes_only_chelans_host_to_the_chelan_scraper():
    from src.scrapers.registry import _detect_template

    assert _detect_template("https://acclaim.co.chelan.wa.us/acclaimweb") is ChelanAcclaimWebScraper
    assert _detect_template("https://edocs.douglascountywa.gov/AcclaimWeb") is AcclaimWebScraper


def test_grid_dates_are_read_in_the_countys_time_zone():
    # 2026-10-08 06:30 UTC is still 10/07 in Wenatchee (23:30 PDT).
    assert _grid_date("/Date(1791441000000)/") == "10/07/2026"
    assert _grid_date("10/07/2026") == "10/07/2026"


def test_grid_item_maps_the_portal_fields():
    row = _PAGES[0]["data"][0]
    item = _grid_item(row)
    assert item["instrument"] == row["InstrumentNumber"]
    assert item["doc_type"] == row["DocType"]
    assert item["legal"] == row["DocLegalDescription"]
    assert item["date_recorded"] == "10/07/2026"


# ── Phase 2b: the parcel comes from the lead's own recording ─────────────────

def _detail(tid):
    return (_FIX / f"detail_{tid}.html").read_text(encoding="utf-8")


def test_the_recording_detail_page_names_its_apn():
    from src.scrapers.templates.acclaimweb import parse_detail_apns

    assert parse_detail_apns(_detail("1789524")) == ["232020860053"]  # TOD
    assert parse_detail_apns(_detail("1788730")) == ["222014846080"]  # NTS


@pytest.mark.parametrize("html,expected", [
    ("Doc Type: D <b>DocLegal:</b> APN 111111111111, 222222222222 LOT 1 <b>Grantor:</b> X",
     ["111111111111", "222222222222"]),
    ("Doc Type: D <b>DocLegal:</b> APN 1111-1111-1111 LOT 1 <b>Grantor:</b> X", ["111111111111"]),
    ("Doc Type: D <b>DocLegal:</b> PLAT OF NOWHERE LOT 1 <b>Grantor:</b> X", []),  # no APN
    # A death certificate's real page shape (live 2026-11-12): no legal, no parcel.
    ("Doc Type: DC - DEATH CERTIFICATE Agency: STATE OF WASHINGTON DEPARTMENT OF HEALTH "
     "Deceased: PARTY1 PERSON", []),
    ("<html>Session expired</html>", None),  # not a detail page: not "no APN"
    # Codex 2b P1: no list separator may hide a second parcel behind the first.
    ("Doc Type: D <b>DocLegal:</b> APN 111111111111 &amp; 222222222222 LOT 1 <b>Grantor:</b> X",
     ["111111111111", "222222222222"]),
    ("Doc Type: D <b>DocLegal:</b> APN 111111111111; 222222222222 <b>Grantor:</b> X",
     ["111111111111", "222222222222"]),
    ("Doc Type: D <b>DocLegal:</b> APN 111111111111 LOT 1 APN 222222222222 <b>Grantor:</b> X",
     ["111111111111", "222222222222"]),
    # Plat and lot numbers in the legal are not parcels.
    ("Doc Type: D <b>DocLegal:</b> APN 232020860053 SP 2019-008 LOT 7 <b>Grantor:</b> X",
     ["232020860053"]),
])
def test_detail_apn_parsing(html, expected):
    from src.scrapers.templates.acclaimweb import parse_detail_apns

    assert parse_detail_apns(html) == expected


async def test_leads_get_their_recordings_apn_and_douglas_is_untouched(browser):
    page = await _portal(browser, _PAGES)
    served = []

    async def detail(route):
        tid = route.request.url.split("/documentdetails/")[1].split("/")[0]
        served.append(tid)
        await route.fulfill(status=200, content_type="text/html", body=_detail("1789524"))

    await page.route(f"{_HOST}/AcclaimWeb/details/documentdetails/**", detail)
    s = _scraper(page, record_type="probate")
    recs = await s._search_and_read("10/07/2026", "10/07/2026")
    assert all(r.enrichment_data.get("source_document_id") for r in recs)
    await s._read_parcels(recs)
    tod = [r for r in recs if r.enrichment_data["source_document_id"] == "1789524"]
    assert tod and tod[0].parcel_id == "232020860053"
    assert tod[0].enrichment_data["recording_apn_lookup"] == "found"
    assert served == ["1789524"]  # one request per recording

    # Douglas rows carry no document id (their hash is unchanged) and nothing is fetched.
    served.clear()
    dpage = await _portal(browser, [_DOUGLAS], first_body="sort=&group=&filter=")
    d = _scraper(dpage, cls=AcclaimWebScraper, record_type="pre_foreclosure")
    drecs = await d._search_and_read("10/06/2026", "10/06/2026")
    assert drecs and not any("source_document_id" in r.enrichment_data for r in drecs)
    assert served == []


async def test_several_apns_on_one_recording_leave_the_parcel_for_review(browser):
    from src.scrapers.base_scraper import ScrapedRecord

    page = await _portal(browser, _PAGES)
    two = "Doc Type: D <b>DocLegal:</b> APN 111111111111, 222222222222 LOT 1 <b>Grantor:</b> X"

    async def detail(route):
        await route.fulfill(status=200, content_type="text/html", body=two)

    await page.route(f"{_HOST}/AcclaimWeb/details/documentdetails/**", detail)
    s = _scraper(page)
    rec = ScrapedRecord(party_name="PARTY1 PERSON", enrichment_data={"source_document_id": "1"})
    await s._read_parcels([rec])
    assert rec.parcel_id is None
    assert rec.enrichment_data["recording_apn_lookup"] == "ambiguous"
    assert rec.enrichment_data["apn_candidates"] == ["111111111111", "222222222222"]


# ── Codex 2b round 2: shape, blocking, breaker, retry, the name-fallback rule ──

async def _detail_portal(browser, answer):
    """A portal page whose recording detail pages answer ``answer(n)`` (status, body) for
    the n-th request; returns (page, requests seen)."""
    page = await _portal(browser, _PAGES)
    seen = []

    async def detail(route):
        seen.append(route.request.url)
        status, body = answer(len(seen))
        await route.fulfill(status=status, content_type="text/html", body=body)

    await page.route(f"{_HOST}/AcclaimWeb/details/documentdetails/**", detail)
    return page, seen


def _recs(n):
    from src.scrapers.base_scraper import ScrapedRecord

    return [ScrapedRecord(party_name=f"PARTY{i} PERSON", enrichment_data={"source_document_id": str(i)})
            for i in range(n)]


async def test_one_apn_of_the_wrong_shape_is_held_for_review(browser):
    page, _ = await _detail_portal(
        browser, lambda n: (200, "Doc Type: D DocLegal: APN 123456789 LOT 1 Grantor: X"))
    s = _scraper(page)
    [rec] = _recs(1)
    await s._read_parcels([rec])
    assert rec.parcel_id is None and rec.enrichment_data["recording_apn_lookup"] == "ambiguous"


async def test_a_refusal_stops_the_run_before_anything_is_saved(browser):
    from src.scrapers.reliability import ScraperBlockedError

    page, seen = await _detail_portal(browser, lambda n: (429, "slow down"))
    s = _scraper(page)
    recs = _recs(4)
    with pytest.raises(ScraperBlockedError):
        await s._read_parcels(recs)
    assert len(seen) == 1  # no retry and no further request after a 429


async def test_an_unreadable_recording_fails_the_run_transiently_not_the_row(browser):
    # A row is never saved half-read: a re-run of the job could not repair it.
    page, seen = await _detail_portal(browser, lambda n: (500, "error"))
    s = _scraper(page)
    recs = _recs(3)
    with pytest.raises(TransientScrapeError, match="unreadable"):
        await s._read_parcels(recs)
    assert len(seen) == 2  # the first recording, tried twice
    assert not any("recording_apn_lookup" in r.enrichment_data for r in recs)


async def test_an_unreadable_200_is_retried(browser):
    page, seen = await _detail_portal(
        browser, lambda n: (200, "<html>Please wait</html>") if n == 1 else (200, _detail("1788730")))
    s = _scraper(page)
    [rec] = _recs(1)
    await s._read_parcels([rec])
    assert len(seen) == 2 and rec.parcel_id == "222014846080"


async def test_one_failure_is_retried_once(browser):
    page, seen = await _detail_portal(
        browser, lambda n: (500, "error") if n == 1 else (200, _detail("1788730")))
    s = _scraper(page)
    [rec] = _recs(1)
    await s._read_parcels([rec])
    assert len(seen) == 2 and rec.parcel_id == "222014846080"


async def test_one_request_per_recording_even_with_several_leads(browser):
    from src.scrapers.base_scraper import ScrapedRecord

    page, seen = await _detail_portal(browser, lambda n: (200, _detail("1789524")))
    s = _scraper(page)
    recs = [ScrapedRecord(party_name=f"P{i}", enrichment_data={"source_document_id": "1789524"})
            for i in range(3)]
    await s._read_parcels(recs)
    assert len(seen) == 1 and {r.parcel_id for r in recs} == {"232020860053"}


@pytest.mark.parametrize("outcome,eligible", [
    (None, True),          # any other county's row: unchanged
    ("none", True),        # a death certificate: the recording names no parcel
    ("found", False),      # the recording's parcel decides the address
    ("ambiguous", False),  # held for review, never a name guess
    ("failed", False),     # unread page: undelivered, not given another identity
])
def test_the_owner_name_fallback_never_overrides_the_recording(outcome, eligible):
    from src.db.models import Result
    from src.workers.tasks_helpers.enrich import name_lookup_eligible

    ed = {"instrument_number": "1"}
    if outcome:
        ed["recording_apn_lookup"] = outcome
    row = Result(party_name="PARTY1 PERSON", property_address=None, enrichment_data=ed)
    assert name_lookup_eligible(row) is eligible
    # A row that already has an address is never looked up.
    row.property_address = "1 MAIN ST"
    assert name_lookup_eligible(row) is False
