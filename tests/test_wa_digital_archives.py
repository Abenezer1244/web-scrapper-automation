"""State Archives court docket index reader (sources/wa_digital_archives).

Fixtures are real Ferry result pages (2026-11-15) with every case title reduced to its
legal words (ESTATE OF / GUARDIANSHIP OF / AND / VS) plus REDACTED, and every
description word that is a name, or rare, replaced by X. Case numbers, dates and
docket codes are as the Archives published them.
"""
from __future__ import annotations

import asyncio
import re
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from bs4 import BeautifulSoup

from src.scrapers.sources import wa_digital_archives as da

FIX = Path(__file__).parent / "fixtures" / "wa_archives_ferry"


def _html(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def _rows(name: str) -> list[da.DocketRow]:
    return da.parse_results_page(_html(name), "ferry")[1]


# ── page parsing ──────────────────────────────────────────────────────────────

def test_a_paged_answer_carries_its_header():
    (page, pages, first, last, total), rows = da.parse_results_page(
        _html("year_2026_p1_of_5.html"), "ferry")
    assert (page, pages, first, last, total) == (1, 5, 1, 200, 825)
    assert len(rows) == 200 and len({r.record_id for r in rows}) == 200
    assert all(r.county == "Ferry" for r in rows)


def test_the_last_page_closes_the_range():
    meta, rows = da.parse_results_page(_html("year_2026_p5_of_5.html"), "ferry")
    assert meta == (5, 5, 801, 825, 825) and len(rows) == 25


def test_a_one_page_answer_without_a_pager_is_counted_by_its_rows():
    meta, rows = da.parse_results_page(_html("case_25-4-00006-10.html"), "ferry")
    assert meta == (1, 1, 1, 7, 7) and {r.case_no for r in rows} == {"25-4-00006-10"}


def test_legacy_dashless_case_numbers_are_kept_but_never_typed():
    legacy = [r for r in _rows("year_2026_p1_of_5.html") if r.case_type is None]
    assert len(legacy) == 3 and all(re.fullmatch(r"\d{4,7}", r.case_no) for r in legacy)


def _set(tag, text: str) -> None:
    tag.clear()
    tag.append(text)


def _mutate(name: str, fn) -> str:
    """The fixture with ``fn(soup, first_data_row_cells)`` applied."""
    soup = BeautifulSoup(_html(name), "html.parser")
    row = soup.find("input", id=re.compile(r"^recordId\d+$")).find_parent("tr")
    fn(soup, row, row.find_all("td", recursive=False))
    return str(soup)


@pytest.mark.parametrize("break_it", [
    lambda soup, row, tds: _set(soup.find(id="pageSummary"), "Pg 1/5"),
    lambda soup, row, tds: row.find("input").attrs.update(id="rowZero"),
    lambda soup, row, tds: _set(tds[1], "2X-1-00001-10"),
    lambda soup, row, tds: _set(tds[2], "2026-01-05"),
    lambda soup, row, tds: tds[-1].decompose(),
], ids=["header", "record id", "case number", "docket date", "cell count"])
def test_anything_unrecognised_is_drift_never_a_short_list(break_it):
    html = _mutate("year_2026_p1_of_5.html", break_it)
    assert html != str(BeautifulSoup(_html("year_2026_p1_of_5.html"), "html.parser"))
    with pytest.raises(da.IndexDriftError):
        da.parse_results_page(html, "ferry")


def test_a_page_with_rows_missing_from_its_range_is_drift():
    html = _mutate("year_2026_p5_of_5.html", lambda soup, row, tds: row.decompose())
    with pytest.raises(da.IndexDriftError):
        da.parse_results_page(html, "ferry")


def test_a_page_of_neither_kind_is_drift():
    with pytest.raises(da.IndexDriftError):
        da.parse_results_page("<html><body>We are sorry but the system has encountered "
                              "an error</body></html>", "ferry")


# ── coverage and classification ──────────────────────────────────────────────

def test_newest_opening_reads_case_information_cover_sheets_only():
    assert da.newest_opening(_rows("year_2026_p1_of_5.html")) is None  # no 2026 filings
    assert da.newest_opening(_rows("case_25-4-00034-10.html")) == date(2025, 10, 20)


def test_an_open_estate():
    case = da.classify_probate(_rows("case_25-4-00006-10.html"))
    assert case is not None and case.status == "open"
    assert case.filed == date(2025, 1, 23) and case.parties == ("REDACTED REDACTED REDACTED",)


def test_a_closed_estate_is_marked_closed():
    assert da.classify_probate(_rows("case_25-4-00012-10.html")).status == "closed"


def test_a_guardianship_is_neither_probate_nor_divorce():
    rows = _rows("case_25-4-00034-10.html")
    assert da.classify_probate(rows) is None and da.classify_divorce(rows) is None


def test_a_dissolution_is_a_divorce_with_both_parties_and_not_probate():
    rows = _rows("case_25-3-00001-10.html")
    case = da.classify_divorce(rows)
    assert case is not None and case.status == "petition_filed"
    assert case.filed == date(2025, 1, 10) and len(case.parties) == 2
    assert da.classify_probate(rows) is None


def test_a_domestic_case_without_a_dissolution_petition_is_not_a_divorce():
    rows = [r for r in _rows("case_25-3-00001-10.html")
            if not r.description.upper().startswith("PETITION FOR DISSOLUTION")]
    assert da.classify_divorce(rows) is None


def test_a_contested_estate_title_is_not_an_estate():
    rows = [da.DocketRow(r.record_id, r.case_no, r.docket_date, r.code, r.description,
                         "ESTATE OF REDACTED V REDACTED", r.county)
            for r in _rows("case_25-4-00006-10.html")]
    assert da.classify_probate(rows) is None


def test_mixed_cases_are_never_classified_together():
    rows = _rows("case_25-4-00006-10.html") + _rows("case_25-4-00012-10.html")
    assert da.classify_probate(rows) is None


# ── the paced search, against a stand-in for the browser page ────────────────

class _Req:
    def __init__(self, url):
        self.url = url


class FakePage:
    """Plays the Archives' browser flow: summary -> title row -> ResultsTable pages."""

    def __init__(self, pages: dict[int, str], summary_total: int | None):
        self.pages, self.total = pages, summary_total
        self.listeners, self.url, self.fetched = [], "", []
        self.submitted = False

    def on(self, event, fn):
        self.listeners.append(fn)

    def remove_listener(self, event, fn):
        self.listeners.remove(fn)

    async def goto(self, url, wait_until=None):
        self.url, self.submitted = url, False

    async def fill(self, sel, value):
        pass

    async def click(self, sel):
        if sel.startswith("form"):
            self.submitted = True
            for fn in self.listeners:
                fn(_Req("https://x/Search/ResultsTable/?id=1&pageSize=0&page=0"))
        else:
            for fn in self.listeners:
                fn(_Req("https://x/Search/ResultsTable/?id=2&pageSize=0&page=0"))

    async def query_selector(self, sel):
        return object() if self.submitted and self.total is not None else None

    async def inner_text(self, sel):
        if sel == "body":
            return da._NO_RESULTS if self.total is None else ""
        return f" {self.total} "

    async def evaluate(self, script, url):
        page = int(parse_qs(urlparse(url).query)["page"][0])
        self.fetched.append(page)
        return self.pages[min(page, max(self.pages))]  # the portal clamps past the end


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    monkeypatch.setattr(da, "_PACE_S", 0)


def _search(page: FakePage, **kw):
    return asyncio.run(da.ArchivesIndex(page, "ferry").search(**kw))


def test_a_search_follows_the_pager_and_reconciles_every_row(monkeypatch):
    p1, p5 = _html("year_2026_p1_of_5.html"), _html("year_2026_p5_of_5.html")
    # Pages 2-4 are not fixtures: a 2-page answer of 225 is built from the two real ends.
    p2 = p5.replace("Page 5 of 5", "Page 2 of 2").replace("Records 801-825 of 825",
                                                          "Records 201-225 of 225")
    p1 = p1.replace("Page 1 of 5", "Page 1 of 2").replace("of 825", "of 225")
    page = FakePage({1: p1, 2: p2}, 225)
    rows = _search(page, year=2026)
    assert len(rows) == 225 and page.fetched == [1, 2]


def test_a_total_that_disagrees_with_the_summary_is_drift():
    page = FakePage({1: _html("case_25-4-00006-10.html")}, 8)
    with pytest.raises(da.IndexDriftError):
        _search(page, case_no="25-4-00006-10")


def test_a_clamped_page_is_caught_not_read_twice():
    p1 = _html("year_2026_p1_of_5.html").replace("Page 1 of 5", "Page 1 of 2")
    page = FakePage({1: p1}, 825)  # asking page 2 returns page 1 again
    with pytest.raises(da.IndexDriftError):
        _search(page, year=2026)


def test_no_results_is_an_explicit_empty_answer():
    assert _search(FakePage({}, None), case_no="25-4-99999-10") == []


def test_search_by_exactly_one_key():
    with pytest.raises(ValueError):
        _search(FakePage({}, None))


def test_an_unknown_county_has_no_index():
    with pytest.raises(ValueError):
        da.ArchivesIndex(FakePage({}, None), "king")


def test_a_page_whose_pager_is_gone_is_counted_by_its_rows():
    """The one-page branch itself: the same real page with its pager removed."""
    html = _mutate("case_25-4-00006-10.html",
                   lambda soup, row, tds: soup.find(id="pageSummary").find_parent("tr").decompose())
    assert "pageSummary" not in html
    meta, rows = da.parse_results_page(html, "ferry")
    assert meta == (1, 1, 1, 7, 7) and len(rows) == 7


def test_a_pagerless_page_still_answers_to_the_summary_total():
    html = _mutate("case_25-4-00006-10.html",
                   lambda soup, row, tds: soup.find(id="pageSummary").find_parent("tr").decompose())
    with pytest.raises(da.IndexDriftError):
        _search(FakePage({1: html}, 8), case_no="25-4-00006-10")


def _estate_with(description: str, code: str = "MT") -> list[da.DocketRow]:
    rows = list(_rows("case_25-4-00006-10.html"))
    last = rows[-1]
    return rows + [da.DocketRow("x" + last.record_id, last.case_no, last.docket_date, code,
                                description, last.title, last.county)]


@pytest.mark.parametrize("description", [
    "MOTION FOR ORDER CLOSING ESTATE", "PROPOSED ORDER CLOSING ESTATE",
    "NOTICE OF HEARING ON CLOSING ESTATE"])
def test_asking_to_close_an_estate_is_not_closing_it(description):
    assert da.classify_probate(_estate_with(description)).status == "open"


@pytest.mark.parametrize("description, code", [
    ("ORDER CLOSING ESTATE", "ORCL"), ("DECLARATION OF COMPLETION", "DCL"),
    ("ANYTHING", "DCLCMP")])
def test_an_executed_closing_document_closes_it(description, code):
    assert da.classify_probate(_estate_with(description, code)).status == "closed"
