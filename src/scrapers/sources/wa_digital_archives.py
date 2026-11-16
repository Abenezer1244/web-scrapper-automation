"""Washington State Archives (Digital Archives) Superior Court docket index.

digitalarchives.wa.gov holds docket rows for 17 county Superior Courts (Ferry first,
collection 3007). Each row: case number, docket date, docket code, description, case
title, county. Search is by docket YEAR or by CASE NUMBER; a year search returns every
docket entry dated that year, for old cases too.

LEGAL STATUS (2026-11-15): GR 31(c)(4) makes dockets and indexes court records and
GR 31(g)(4) bars "commercial solicitation of individuals named in the court records"
when those records are distributed in bulk. Connectors built on this module stay
INACTIVE until counsel clears that use (tasks/ferry-wa-seven.md).

What the index is, measured on Ferry 2026-11-15: docket entries run to 2026-06-12, the
newest case OPENING (docket code CICS, the case information cover sheet) is from
November 2025, and no 2026-numbered case is present. The index trails the clerk by
months, so every reader here proves coverage before it answers (``newest_opening``).

Mechanics (verified): POST /Collections/Search, then the per-title row of the result
summary, then GET /Search/ResultsTable pages. Only the browser flow returns pages (the
same requests without the page's script state answer HTTP 500), so this runs in
Playwright. Pages are 1-based and CLAMP: asking past the end returns the last page
again, so paging follows the page's own "Page X of Y, Records a-b of N" header and the
rows are reconciled against N by their record ids (three rows on one Ferry page carry
identical visible text). robots.txt disallows /Record/ only; nothing here goes there.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import date, datetime

from bs4 import BeautifulSoup

from src.scrapers.reliability import ScraperExecutionError

ORIGIN = "https://digitalarchives.wa.gov"
# County -> the Archives title id of its Superior Court case files.
TITLES: dict[str, int] = {"ferry": 3007}
_RECORD_SERIES = 26                      # "Superior Court Cases"
PAGE_SIZE = 500
_PACE_S = 2.0
_RESULT_WAIT_S = 90
_CASE_NO = re.compile(r"^\d{2}-(\d)-\d{5}-\d{1,2}$")
# Some old rows print the case number without its dashes ("17143" for 17-1-00043):
# kept and counted, never classified (no case type can be read off them).
_LEGACY_CASE_NO = re.compile(r"^\d{4,7}$")
_HEADER = re.compile(r"Page\s+(\d+)\s+of\s+(\d+),\s+Records\s+(\d+)-(\d+)\s+of\s+(\d+)")
_NO_RESULTS = "No search results found."
OPENING_CODE = "CICS"                    # case information cover sheet: the filing


@dataclass(frozen=True)
class DocketRow:
    record_id: str
    case_no: str
    docket_date: date
    code: str
    description: str
    title: str
    county: str

    @property
    def case_type(self) -> str | None:
        """WA case-type digit: 1 criminal, 2 civil, 3 domestic, 4 probate/guardianship;
        None for a legacy dash-less number."""
        m = _CASE_NO.match(self.case_no)
        return m.group(1) if m else None


class IndexDriftError(ScraperExecutionError):
    """The index answered with something this reader does not recognise."""


def _drift(county: str, msg: str) -> IndexDriftError:
    return IndexDriftError(county, "wa_digital_archives", msg)


def parse_results_page(html: str, county: str) -> tuple[tuple[int, int, int, int, int], list[DocketRow]]:
    """((page, pages, first, last, total), rows) for one ResultsTable page.

    Raises IndexDriftError on a missing header, a row of an unknown shape, a malformed
    case number or date, or a row count that disagrees with the header's range."""
    soup = BeautifulSoup(html, "html.parser")
    text = " ".join(soup.get_text(" ").split())
    head = _HEADER.search(text)
    # A pager we cannot read is drift; only a page with NO pager is a one-page answer.
    if head is None and (soup.find(id="pageSummary") is not None
                         or "Case Number Date Y/M/D Date Docket Code" not in text):
        raise _drift(county, "results page without its 'Page X of Y' header")
    rows: list[DocketRow] = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        box = tr.find("input", id=re.compile(r"^recordId\d+$"))
        if box is None:
            if len(tds) == 10:
                raise _drift(county, "a result row without its record id")
            continue
        if len(tds) != 10:
            raise _drift(county, f"a result row with {len(tds)} cells")
        cells = [" ".join(td.get_text(" ").split()) for td in tds]
        case_no, ymd, _, code, desc, title, _, row_county = cells[1:9]
        if not (_CASE_NO.match(case_no) or _LEGACY_CASE_NO.match(case_no)):
            raise _drift(county, "a malformed case number")
        try:
            when = datetime.strptime(ymd, "%Y/%m/%d").date()
        except ValueError as exc:
            raise _drift(county, "a malformed docket date") from exc
        rows.append(DocketRow(box.get("value") or "", case_no, when, code, desc, title, row_county))
    if head is None:
        # A one-page answer prints no pager; the caller still reconciles the row count
        # against the search summary's total.
        page, pages, first, last, total = 1, 1, 1, len(rows), len(rows)
    else:
        page, pages, first, last, total = (int(g) for g in head.groups())
    if len(rows) != last - first + 1:
        raise _drift(county, f"page {page} lists {len(rows)} rows for records {first}-{last}")
    if any(not r.record_id for r in rows):
        raise _drift(county, "a result row with an empty record id")
    return (page, pages, first, last, total), rows


def newest_opening(rows: list[DocketRow]) -> date | None:
    """The latest case opening (CICS) the rows show: how far the index's NEW filings reach."""
    return max((r.docket_date for r in rows if r.code == OPENING_CODE), default=None)


class ArchivesIndex:
    """One Playwright page reading one county's docket index; requests paced."""

    def __init__(self, page, county: str):
        if county not in TITLES:
            raise ValueError(f"no Archives title for {county}")
        self._page = page
        self._county = county
        self._title = TITLES[county]

    async def search(self, *, year: int | None = None, case_no: str | None = None) -> list[DocketRow]:
        """Every docket row for one year or one case, reconciled to the reported total."""
        if (year is None) == (case_no is None):
            raise ValueError("search by exactly one of year or case_no")
        pg = self._page
        tables: list[str] = []      # ResultsTable requests: the summary's, then the title's

        def _seen(req) -> None:
            if "/Search/ResultsTable/" in req.url:
                tables.append(req.url)

        pg.on("request", _seen)
        try:
            return await self._search(tables, year, case_no)
        finally:
            pg.remove_listener("request", _seen)

    async def _search(self, tables: list[str], year, case_no) -> list[DocketRow]:
        pg, county = self._page, self._county
        await asyncio.sleep(_PACE_S)  # one search at a time, spaced, per county
        await pg.goto(f"{ORIGIN}/Collections/TitleInfo/{self._title}", wait_until="networkidle")
        if case_no:
            await pg.fill("#CaseNumber", case_no)
        else:
            await pg.fill("#StartYear", str(year))
            await pg.fill("#EndYear", str(year))
        await pg.click("form[action='/Collections/Search'] [type=submit], "
                       "form[action='/Collections/Search'] button")
        row_sel = f"tr[onclick*=',{self._title})']"
        found = await self._wait_for(row_sel)
        if not found:
            return []                                   # the page said "no results"
        expected = int((await pg.inner_text(f"{row_sel} td:nth-child(2)")).strip().replace(",", ""))
        ids: list[str] = []
        before = len(tables)
        await pg.click(row_sel)
        sid = await self._results_id(tables, before)
        rows: dict[str, DocketRow] = {}
        page_no = 1
        while True:
            await asyncio.sleep(_PACE_S)
            html = await pg.evaluate(
                "u => fetch(u).then(r => r.ok ? r.text() : Promise.reject(r.status))",
                f"/Search/ResultsTable/?id={sid}&sortColumn=null&direction=null"
                f"&pageSize={PAGE_SIZE}&page={page_no}")
            (page, pages, _, _, total), got = parse_results_page(html, county)
            if page != page_no or total != expected:
                raise _drift(county, f"asked page {page_no}, got page {page} ({total} vs {expected})")
            for r in got:
                if r.county.lower() != county:
                    raise _drift(county, "a row from another county")
                rows[r.record_id] = r
                ids.append(r.record_id)
            if page >= pages:
                break
            page_no += 1
        if len(rows) != expected or len(ids) != expected:
            raise _drift(county, f"{len(rows)} unique rows for {expected} reported")
        return list(rows.values())

    async def _wait_for(self, row_sel: str) -> bool:
        pg = self._page
        from playwright.async_api import Error as PlaywrightError

        for _ in range(_RESULT_WAIT_S):
            try:
                if await pg.query_selector(row_sel):
                    return True
                if _NO_RESULTS in await pg.inner_text("body"):
                    return False
            except PlaywrightError:
                pass  # the submit navigates (/Collections/Search -> /Search): poll again
            await asyncio.sleep(1)
        raise _drift(self._county, "the search never answered")

    async def _results_id(self, tables: list[str], before: int) -> str:
        """The search id the title's own result table was requested with."""
        for _ in range(_RESULT_WAIT_S):
            if len(tables) > before:
                sid = re.search(r"[?&]id=(\d+)", tables[-1])
                if sid:
                    return sid.group(1)
            await asyncio.sleep(1)
        raise _drift(self._county, "the result table never loaded")


# ── classification ────────────────────────────────────────────────────────────

_ESTATE_TITLE = re.compile(r"^(?:IN RE:?\s+(?:THE\s+)?)?ESTATE OF\s+(.+)$")
_ADVERSE = re.compile(r"\s(?:VS?\.?|V\.)\s")
# Executed closing documents only: a MOTION, PROPOSED order or NOTICE about closing is
# not a closed estate (Codex). DCLCMP is the completion declaration's own docket code.
_CLOSED = re.compile(r"^(?:DECLARATION OF COMPLETION|ORDER CLOSING|DECREE CLOSING)\b")
_CLOSED_CODES = frozenset({"DCLCMP"})
_DISSOLUTION_PETITION = re.compile(r"^PETITION FOR DISSOLUTION\b")


@dataclass(frozen=True)
class CourtCase:
    case_no: str
    filed: date                 # the CICS opening
    title: str
    parties: tuple[str, ...]
    status: str                 # probate: open|closed; divorce: petition_filed


def classify_probate(history: list[DocketRow]) -> CourtCase | None:
    """An estate case (open or closed) from its full docket history; None otherwise.

    Affirmative only: case type 4, an "ESTATE OF <name>" title with no adverse party,
    and an opening entry. Guardianships, "IN RE <name>" and contested matters are not
    estates here. Closed means a completion declaration or closing order is docketed;
    otherwise "open" as far as the index reaches (which trails the clerk)."""
    case = _single_case(history)
    if case is None or case[0].case_type != "4":
        return None
    title = case[0].title.upper()
    m = _ESTATE_TITLE.match(title)
    filed = min((r.docket_date for r in case if r.code == OPENING_CODE), default=None)
    if m is None or _ADVERSE.search(" " + title + " ") or filed is None:
        return None
    closed = any(r.code in _CLOSED_CODES or _CLOSED.match(r.description.upper()) for r in case)
    return CourtCase(case[0].case_no, filed, case[0].title, (m.group(1).strip(),),
                     "closed" if closed else "open")


def classify_divorce(history: list[DocketRow]) -> CourtCase | None:
    """A dissolution of marriage from its full docket history; None otherwise.

    Affirmative only: case type 3 and a docketed PETITION FOR DISSOLUTION. Legal
    separation, invalidity, parenting plans, support modification and state-filed
    support cases are not divorces."""
    case = _single_case(history)
    if case is None or case[0].case_type != "3":
        return None
    title = case[0].title.upper()
    if title.startswith("STATE OF WASHINGTON") or title.startswith("STATE "):
        return None
    if not any(_DISSOLUTION_PETITION.match(r.description.upper()) for r in case):
        return None
    filed = min((r.docket_date for r in case if r.code == OPENING_CODE), default=None)
    if filed is None:
        return None
    parties = tuple(p.strip() for p in re.split(r"\s+(?:AND|VS?\.?)\s+", title) if p.strip())
    return CourtCase(case[0].case_no, filed, case[0].title, parties, "petition_filed")


def _single_case(history: list[DocketRow]) -> list[DocketRow] | None:
    if not history or len({r.case_no for r in history}) != 1:
        return None
    return history
