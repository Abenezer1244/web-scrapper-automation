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
    DouglasAcclaimWebScraper,
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
    s = _scraper(page, cls=DouglasAcclaimWebScraper, record_type="pre_foreclosure")
    s.county = "douglas"
    recs = await s._search_and_read("10/06/2026", "10/06/2026")
    lis = [r for r in _DOUGLAS["Data"] if r["DocTypeDescription"] == "LIS PENDENS"]
    assert lis, "fixture control: the day has a lis pendens"
    assert len(recs) == len(lis)
    assert recs[0].doc_type == "LIS PENDENS"
    assert recs[0].date_recorded == "10/06/2026"
    # The lead carries its recording's own 11-digit parcel (the billing key Douglas's
    # delivered leads were hashed with).
    assert len(lis[0]["ParcelNumber"]) == 11, "fixture control"
    assert recs[0].parcel_id == lis[0]["ParcelNumber"]
    assert "grid_parcel_unmatched" not in recs[0].enrichment_data


async def test_douglas_grid_parcel_of_another_shape_is_kept_for_review_not_keyed(browser):
    data = copy.deepcopy(_DOUGLAS)
    lis = next(r for r in data["Data"] if r["DocTypeDescription"] == "LIS PENDENS")
    lis["ParcelNumber"] = "123456789012"  # 12 digits: not a Douglas parcel
    page = await _portal(browser, [data], first_body="sort=&group=&filter=")
    s = _scraper(page, cls=DouglasAcclaimWebScraper, record_type="pre_foreclosure")
    recs = await s._search_and_read("10/06/2026", "10/06/2026")
    assert recs[0].parcel_id is None
    assert recs[0].enrichment_data["grid_parcel_unmatched"] == "123456789012"


async def test_douglas_lists_a_recording_twice_and_it_is_read_once(browser):
    # Live 10/19/2026: 50 rows, Total 50, one SURVEYS FOR AUDITOR listed twice (only GUID differs).
    data = copy.deepcopy(_DOUGLAS)
    lis = next(r for r in data["Data"] if r["DocTypeDescription"] == "LIS PENDENS")
    data["Data"].append({**lis, "GUID": "another-guid"})
    data["Total"] = len(data["Data"])
    page = await _portal(browser, [data], first_body="sort=&group=&filter=")
    s = _scraper(page, cls=DouglasAcclaimWebScraper, record_type="pre_foreclosure")
    recs = await s._search_and_read("10/06/2026", "10/06/2026")
    assert len(recs) == 1


async def test_a_paged_grid_repeating_an_identical_row_still_fails(browser):
    # Paged (Chelan): a repeat can be a repeated page hiding a missed row, so no collapse.
    pages = copy.deepcopy(_PAGES)
    pages[3]["data"].append({**pages[3]["data"][0], "GUID": "another-guid"})
    for p in pages:
        p["total"] = 44
    s = _scraper(await _portal(browser, pages))
    with pytest.raises(ScraperExecutionError, match="distinct"):
        await s._search_and_read("10/07/2026", "10/07/2026")


_KENDO_SINGLE = """<html><body><input id="RecordDatePicker">
<script>
const picker = {value(v) { document.getElementById('RecordDatePicker').value = v; }, trigger() {}};
window.$ = (sel) => ({data: (k) => (sel === '#RecordDatePicker' && k === 'kendoDatePicker') ? picker : undefined});
</script></body></html>"""


class _SearchesThroughFillDates(DouglasAcclaimWebScraper):
    """The real _fill_dates on a page whose only date field is a Kendo single picker."""

    async def navigate(self, url):
        self.searched = []
        await self.page.goto(url)

    async def _accept_disclaimer(self):
        pass

    async def _go_to_search(self):
        pass

    async def _search_and_read(self, date_from, date_to):
        self.searched.append(await self.page.input_value("#RecordDatePicker"))
        return []


async def test_a_kendo_single_date_site_searches_every_day_of_the_window(browser):
    page = await browser.new_page()
    await page.route(f"{_SEARCH}*", lambda r: r.fulfill(status=200, content_type="text/html",
                                                        body=_KENDO_SINGLE))
    s = _SearchesThroughFillDates(f"{_HOST}/AcclaimWeb", "douglas", "WA",
                                  record_types=["pre_foreclosure"], record_type="pre_foreclosure")
    s.page = page
    await s.scrape("10/01/2026", "10/09/2026")
    # Pre-detection only knows #RecordDate; the fill finds the single picker and the loop
    # must then step one day at a time through the end date (Codex), not a week.
    assert s.searched == [f"10/0{d}/2026" for d in range(1, 10)]


async def test_a_repeated_item_that_differs_is_still_a_failed_read(browser):
    data = copy.deepcopy(_DOUGLAS)
    lis = next(r for r in data["Data"] if r["DocTypeDescription"] == "LIS PENDENS")
    data["Data"].append({**lis, "GUID": "another-guid", "DirectName": "SOMEONE ELSE"})
    data["Total"] = len(data["Data"])
    page = await _portal(browser, [data], first_body="sort=&group=&filter=")
    s = _scraper(page, cls=DouglasAcclaimWebScraper, record_type="pre_foreclosure")
    with pytest.raises(ScraperExecutionError, match="distinct"):
        await s._search_and_read("10/06/2026", "10/06/2026")


class _NoDateFieldYet:
    """A page whose date fields are not on it yet when scrape() looks (live Douglas 2026-11-15)."""
    url = "https://edocs.douglascountywa.gov/AcclaimWeb/search/SearchTypeRecordDate"

    def locator(self, _sel):
        raise RuntimeError("not loaded")


class _RecordsSearches(DouglasAcclaimWebScraper):
    async def navigate(self, url):
        self.searched = []

    async def _accept_disclaimer(self):
        pass

    async def _go_to_search(self):
        pass

    async def _fill_dates(self, date_from, date_to):
        pass

    async def _search_and_read(self, date_from, date_to):
        self.searched.append((date_from, date_to))
        return []


@pytest.mark.parametrize("window,expected", [
    (("10/19/2026", "10/19/2026"), [("10/19/2026", "10/19/2026")]),
    (("10/01/2026", "10/08/2026"), [("10/01/2026", "10/08/2026")]),
])
async def test_a_window_is_always_searched_even_when_its_date_mode_was_not_detected(window, expected):
    s = _RecordsSearches("https://edocs.douglascountywa.gov/AcclaimWeb", "douglas", "WA",
                         record_types=["pre_foreclosure"], record_type="pre_foreclosure")
    s.page = _NoDateFieldYet()
    assert await s.scrape(*window) == []
    assert s.searched == expected  # never zero searches reported as a zero


async def test_other_acclaimweb_sites_never_key_on_the_grid_parcel(browser):
    page = await _portal(browser, [_DOUGLAS], first_body="sort=&group=&filter=")
    s = _scraper(page, cls=AcclaimWebScraper, record_type="pre_foreclosure")
    recs = await s._search_and_read("10/06/2026", "10/06/2026")
    assert recs and all(r.parcel_id is None for r in recs)


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
    assert item["parcel"] == ""  # only a scraper with GRID_PARCEL_SHAPE reads grid_parcel
    assert item["grid_parcel"] == str(_DOUGLAS["Data"][0].get("ParcelNumber") or "")


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


def test_the_shared_vocabulary_is_unchanged():
    assert AcclaimWebScraper.DOC_TYPE_MAP is _DOC_TYPE_MAP


def test_douglas_pre_foreclosure_is_the_shared_map_that_doc_types_partitions():
    assert DouglasAcclaimWebScraper.DOC_TYPE_MAP["pre_foreclosure"] == _DOC_TYPE_MAP["pre_foreclosure"]


@pytest.mark.parametrize("desc", ["AFFIT OF HEIRSHIP", "LETTER OF TESTAMENTARY",
                                  "LACK OF PROBATE AFFIDAVIT", "LETTERS OF ADMINISTRATION",
                                  "AFFIDAVIT OF DEATH", "LAST WILL AND TESTAMENT", "DEATH CERTIFICATE"])
def test_douglas_probate_document_names_from_its_live_list(desc):
    assert _doc_type_matches(desc, DouglasAcclaimWebScraper.DOC_TYPE_MAP["probate"])


@pytest.mark.parametrize("desc", ["REAL ESTATE CONTRACT", "DEED OF TRUST", "TRUST"])
def test_douglas_probate_terms_do_not_admit_ordinary_documents(desc):
    assert not _doc_type_matches(desc, DouglasAcclaimWebScraper.DOC_TYPE_MAP["probate"])


@pytest.mark.parametrize("desc,hit", [("DEATH CERTIFICATE", True), ("AFFIDAVIT OF DEATH", False),
                                      ("TRANSFER ON DEATH DEED", False), ("AFFI DEATH JOINT TENANT", False)])
def test_douglas_death_certificate_is_the_recorded_certificate_only(desc, hit):
    assert _doc_type_matches(desc, DouglasAcclaimWebScraper.DOC_TYPE_MAP["death_certificate"]) is hit


def test_death_certificate_scope_is_the_recorders_own_document_type():
    for cls in (ChelanAcclaimWebScraper, DouglasAcclaimWebScraper):
        scope = cls.collection_scope("death_certificate")
        assert scope.kind == "document_type"
        assert [i.label for i in scope.items] == ["Death Certificate"]
    # A site with no death_certificate vocabulary does not claim the document type.
    generic = AcclaimWebScraper.collection_scope("death_certificate")
    assert generic is None or generic.kind != "document_type"


async def test_douglas_death_certificate_names_the_decedent_and_keeps_its_parcel(browser):
    data = copy.deepcopy(_DOUGLAS)
    dc = copy.deepcopy(data["Data"][0])
    dc.update(DocTypeDescription="DEATH CERTIFICATE", DirectName="STATE OF WASHINGTON",
              IndirectName="DOE JOHN A ESTATE OF", ParcelNumber="01234567890",
              InstrumentNumber="9990001", TransactionItemId=9990001)
    data["Data"].append(dc)
    data["Total"] = len(data["Data"])
    page = await _portal(browser, [data], first_body="sort=&group=&filter=")
    s = _scraper(page, cls=DouglasAcclaimWebScraper, record_type="death_certificate")
    recs = await s._search_and_read("10/06/2026", "10/06/2026")
    assert len(recs) == 1
    assert recs[0].party_name == "DOE JOHN A"
    assert recs[0].parcel_id == "01234567890"  # leading zero kept, string


def test_douglas_release_of_lis_pendens_is_not_a_lead():
    from src.scrapers.preforeclosure import is_cancellation_or_admin

    assert _doc_type_matches("RELEASE LIS PENDENS", _DOC_TYPE_MAP["pre_foreclosure"])  # control
    assert is_cancellation_or_admin("RELEASE LIS PENDENS")
    assert not is_cancellation_or_admin("LIS PENDENS")


def test_registry_routes_chelan_and_douglas_hosts_to_their_scrapers():
    from src.scrapers.registry import _detect_template

    assert _detect_template("https://acclaim.co.chelan.wa.us/acclaimweb") is ChelanAcclaimWebScraper
    assert _detect_template("https://edocs.douglascountywa.gov/AcclaimWeb") is DouglasAcclaimWebScraper
    assert _detect_template("https://edocs.douglascountywa.gov.evil.example/AcclaimWeb") is AcclaimWebScraper


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


# ── Phase 3: death_certificate (Chelan recorder "DC"), code only ─────────────

def test_chelan_death_certificate_type_takes_dc_only_and_names_the_decedent():
    base = _PAGES[0]["data"]
    dc = dict(base[0], DocType="DC", DirectName="STATE OF WASHINGTON DEPARTMENT OF HEALTH",
              IndirectName="PARTY9 PERSON", TransactionItemId=999)
    tod = next(r for p in _PAGES for r in p["data"] if r["DocType"] == "TOD")
    s = ChelanAcclaimWebScraper(f"{_HOST}/AcclaimWeb", "chelan", "wa",
                                record_types=["death_certificate"], record_type="death_certificate")
    recs = s._records_from_raw([_grid_item(dc), _grid_item(tod)])
    assert [r.doc_type for r in recs] == ["DC"]
    assert recs[0].party_name == "PARTY9 PERSON"  # the decedent, not the issuing agency
    assert ChelanAcclaimWebScraper.collection_scope("death_certificate") is not None


async def test_a_type_the_site_has_no_vocabulary_for_is_refused_not_unfiltered():
    # Codex P3 review: Douglas has no death_certificate codes; running it would have
    # skipped the document filter and admitted every recording.
    d = AcclaimWebScraper("https://edocs.douglascountywa.gov/AcclaimWeb", "douglas", "wa",
                          record_types=["death_certificate"], record_type="death_certificate")
    with pytest.raises(ScraperExecutionError, match="no document vocabulary"):
        await d.scrape("10/06/2026", "10/06/2026")
    assert d.page is None  # refused before any browser or portal request


# ── Douglas's slow portal: a stalled search is retried once on a fresh form ─────

class _FillsRecorded(DouglasAcclaimWebScraper):
    """The real Douglas search, retry and form reload; the date fill is recorded (the
    fixture page has no date inputs)."""

    async def _fill_dates(self, date_from, date_to):
        self.filled = (date_from, date_to)


async def _douglas_portal(browser, data=None, *, shell=None):
    """Douglas's fixture portal, counting form loads and grid reads. ``shell(n, route)``
    may answer the n-th grid shell itself (return True) or leave it to the portal."""
    page = await _portal(browser, [data or _DOUGLAS], first_body="sort=&group=&filter=")
    seen = {"shells": 0, "grids": 0, "forms": 0}

    async def on_shell(route):
        seen["shells"] += 1
        if not (shell and await shell(seen["shells"], route)):
            await route.fallback()

    async def on_grid(route):
        seen["grids"] += 1
        await route.fallback()

    async def on_form(route):
        seen["forms"] += 1
        await route.fallback()

    await page.route(f"{_HOST}/AcclaimWeb/Search/PartialGrid*", on_shell)
    await page.route(f"{_HOST}/AcclaimWeb/Search/GridResults*", on_grid)
    await page.route(f"{_SEARCH}*", on_form)
    return page, seen


def _fail_first(k):
    async def shell(n, route):
        if n <= k:
            await route.fulfill(status=503, body="busy")
            return True
        return False
    return shell


@pytest.fixture
def no_pause(monkeypatch):
    import src.scrapers.templates.acclaimweb as aw

    monkeypatch.setattr(aw, "_DOUGLAS_RETRY_PAUSE_S", 0)
    return aw


_LIS = [r for r in _DOUGLAS["Data"] if r["DocTypeDescription"] == "LIS PENDENS"]


async def test_douglas_reads_the_day_after_one_stalled_search_on_a_new_page(browser, no_pause):
    page, seen = await _douglas_portal(browser, shell=_fail_first(1))
    s = _scraper(page, cls=_FillsRecorded, record_type="pre_foreclosure")
    recs = await s._search_and_read("10/06/2026", "10/06/2026")
    assert len(recs) == len(_LIS) >= 1
    assert seen["shells"] == 2 and seen["forms"] == 1  # the form was loaded anew
    assert s.filled == ("10/06/2026", "10/06/2026")


async def test_douglas_two_stalled_searches_fail_the_day_not_skip_it(browser, no_pause):
    page, seen = await _douglas_portal(browser, shell=_fail_first(2))
    s = _scraper(page, cls=_FillsRecorded, record_type="pre_foreclosure")
    with pytest.raises(TransientScrapeError):
        await s._search_and_read("10/06/2026", "10/06/2026")
    assert seen["shells"] == 2  # one retry, no more


async def test_douglas_retries_once_per_run_not_once_per_day(browser, no_pause):
    async def first_and_third(n, route):
        if n in (1, 3):
            await route.fulfill(status=503, body="busy")
            return True
        return False

    page, seen = await _douglas_portal(browser, shell=first_and_third)
    s = _scraper(page, cls=_FillsRecorded, record_type="pre_foreclosure")
    assert await s._search_and_read("10/06/2026", "10/06/2026")
    with pytest.raises(TransientScrapeError):  # a later stalled day is not retried again
        await s._search_and_read("10/06/2026", "10/06/2026")
    assert seen["shells"] == 3 and seen["forms"] == 1


async def test_a_late_answer_to_the_stalled_search_is_never_read_as_the_retry(browser, monkeypatch, no_pause):
    """The stalled search's shell answers after its wait gave up; on the old page that
    answer would post a grid read the retry could take for its own (Codex). The retry's
    new page means it never posts."""
    monkeypatch.setattr(no_pause, "_DOUGLAS_SHELL_TIMEOUT_S", 1)

    async def late_first(n, route):
        if n == 1:
            await asyncio.sleep(2.5)
            try:
                await route.fallback()
            except Exception:  # noqa: BLE001 -- the page that asked is gone
                pass
            return True
        return False

    page, seen = await _douglas_portal(browser, shell=late_first)
    s = _scraper(page, cls=_FillsRecorded, record_type="pre_foreclosure")
    recs = await s._search_and_read("10/06/2026", "10/06/2026")
    await page.wait_for_timeout(3_000)  # past the late answer
    assert len(recs) == len(_LIS)
    assert seen["grids"] == 1  # only the retry's own grid read


async def test_douglas_schema_drift_is_not_retried(browser, no_pause):
    data = copy.deepcopy(_DOUGLAS)
    data["Total"] = len(data["Data"]) + 5  # rows short of the portal's own count
    page, seen = await _douglas_portal(browser, data)
    s = _scraper(page, cls=_FillsRecorded, record_type="pre_foreclosure")
    with pytest.raises(ScraperExecutionError) as err:
        await s._search_and_read("10/06/2026", "10/06/2026")
    assert not isinstance(err.value, TransientScrapeError)
    assert seen["forms"] == 0


async def test_douglas_waits_past_the_shared_30s_for_its_grid(browser, no_pause, monkeypatch):
    # Scaled down: the shell answers after 1.5 s; the caller's wait would be 1 s.
    monkeypatch.setattr(no_pause, "_DOUGLAS_SHELL_TIMEOUT_S", 3)

    async def slow(n, route):
        await asyncio.sleep(1.5)
        return False

    page, seen = await _douglas_portal(browser, shell=slow)
    s = _scraper(page, cls=_FillsRecorded, record_type="pre_foreclosure")
    assert await s._search_rows("10/06/2026", "10/06/2026", shell_timeout_s=1)
    assert seen["forms"] == 0


async def test_douglas_falls_back_to_the_shared_wait_once_its_slow_budget_is_spent(browser, no_pause, monkeypatch):
    # Scaled down: long wait 3 s, shared wait 1 s, shell answers after 1.5 s.
    monkeypatch.setattr(no_pause, "_DOUGLAS_SHELL_TIMEOUT_S", 3)
    monkeypatch.setattr(no_pause, "_DOUGLAS_SLOW_BUDGET_S", 1)

    async def slow(n, route):
        await asyncio.sleep(1.5)
        return False

    page, seen = await _douglas_portal(browser, shell=slow)
    s = _scraper(page, cls=_FillsRecorded, record_type="pre_foreclosure")
    assert await s._search_rows("10/06/2026", "10/06/2026", shell_timeout_s=1)  # in budget
    with pytest.raises(TransientScrapeError):  # budget spent: the 1 s shared wait applies
        await s._search_rows("10/06/2026", "10/06/2026", shell_timeout_s=1)


class SoftTimeLimitExceeded(Exception):
    """Named as Celery's: the scraper layer matches its time limits by class name."""


class _OutOfTimeMidSearch(_FillsRecorded):
    async def _submit_search(self):
        raise SoftTimeLimitExceeded()


async def test_running_out_of_time_mid_search_is_not_a_stall_and_is_not_retried(browser, no_pause):
    page, seen = await _douglas_portal(browser)
    s = _scraper(page, cls=_OutOfTimeMidSearch, record_type="pre_foreclosure")
    with pytest.raises(SoftTimeLimitExceeded):
        await s._search_and_read("10/06/2026", "10/06/2026")
    assert seen["forms"] == 0
