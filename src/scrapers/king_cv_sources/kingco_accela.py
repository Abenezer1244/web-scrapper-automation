"""Unincorporated King County code enforcement cases from the county's Accela Citizen Access.

Source: aca-prod.accela.com/KINGCO, Enforcement module, General Search with record type
"Code Enforcement Case" (value Enforce/Enforcement/NA/NA) over an opened-date range. The
portal has no API, so it is driven with headless Playwright through BridgeScraper (SSRF
route guard, resolved browser identity). Public, no login; robots.txt is a 404.

Portal behavior verified live on 2026-09-14:
  * The search is an ASP.NET AJAX postback. Filling the date inputs with fill() sends the
    portal to Error.aspx, so dates are typed key by key and committed with Tab.
  * The server session keeps the previous search's results, so every search window runs
    in a fresh browser context (a new session).
  * A search with exactly one match skips the grid and opens that case's detail page,
    which prints no opened date; a multi-day window that does this is searched again day
    by day (verified on 09/13/2026, one case).
  * A results page is swapped in after the postback's network activity settles, so each
    page waits for the "Showing" range to advance before it is read.
  * Results list 10 rows a page. "Showing 1-10 of 100+" is only a display cap (paging on
    reaches "101-110 of 122"), but windows are kept to WINDOW_DAYS days anyway.
  * The grid carries the opened date, case number, record type, status and a street+ZIP
    address with no city ("7016 S LAKERIDGE DR, 98178"). The street is stored as
    property_address and the ZIP as the record's property_zip; the city is left to the
    parcel enrichment (a "STREET ZIP" line would reach skip trace with the ZIP parsed
    into the street). The King PIN is only on the case detail page (Parcel Information). Cases still
    in "Intake Processing" often have no parcel yet: they are kept with parcel_id None.
    A case listing more than one distinct parcel is also kept with parcel_id None (the
    PINs are recorded) because no single parcel's owner is provably the right one.

PRIVACY. The grid's Description column and the detail page's Project Description,
Additional Information and Application Information are complainant free text (names,
home addresses, children). None of it is read into a record, stored or exported. The
staff "Short Notes" column is not read either. violation_category is None: the portal has
one record type and no category field, and the only category-like text is the
complainant's description.

PACING AND BUDGET. Every navigation or postback waits PACE_SECONDS after the previous one.
A case costs one detail page, so a run looks up at most MAX_DETAIL_PAGES cases and spends
at most TIME_BUDGET_SECONDS. A range over either bound fails this source loudly (the
connector ships the other jurisdictions with a warning) instead of shipping some cases
with a parcel and some without, which would give the same case a different billing
identity on a later run.

A login, captcha or terms-acceptance page is never worked around: it raises
AccelaAccessWallError, which is not retried.
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from src.api.middleware.security import add_scrape_domain
from src.config import settings
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.scrapers.king_cv_sources import KINGCO_ACCELA
from src.scrapers.king_cv_sources.base import (
    LABEL_MAX,
    MAX_PAGES,
    CodeViolationSource,
    DateRangeTooLargeError,
    normalize_king_pin,
)
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.king_cv_sources.kingco_accela")

_HOST = "aca-prod.accela.com"
_ORIGIN = f"https://{_HOST}"
SEARCH_URL = f"{_ORIGIN}/KINGCO/Cap/CapHome.aspx?module=Enforce&TabName=Enforce"
add_scrape_domain(_HOST)

RECORD_TYPE_VALUE = "Enforce/Enforcement/NA/NA"
WINDOW_DAYS = 7
PACE_SECONDS = 3.0
# Measured live on 2026-09-14: the last 30 days (122 cases, 13 results pages) took 556 s,
# about 4.5 s a case. The worker cancels the WHOLE scrape at 30 minutes (tasks.py
# _SCRAPE_TIMEOUT), which would lose the other jurisdictions too, so this source stops
# itself well before: at most MAX_DETAIL_PAGES cases (about 15 minutes) and at most
# TIME_BUDGET_SECONDS of wall clock, whichever comes first.
MAX_DETAIL_PAGES = 200
TIME_BUDGET_SECONDS = 1200

_FORM = "ctl00_PlaceHolderMain_generalSearchForm_"
SEL_RECORD_TYPE = f"#{_FORM}ddlGSPermitType"
SEL_START_DATE = f"#{_FORM}txtGSStartDate"
SEL_END_DATE = f"#{_FORM}txtGSEndDate"
SEL_SEARCH = "#ctl00_PlaceHolderMain_btnNewSearch"
SEL_GRID = "#ctl00_PlaceHolderMain_dgvPermitList_gdvPermitList"
SEL_NO_RESULTS = "#ctl00_PlaceHolderMain_RecordSearchResultInfo_noDataMessageForSearchResultList_lblMessage"
SEL_NEXT = "tr.ACA_Table_Pages a:has-text('Next')"
SEL_DETAIL_CASE = "#ctl00_PlaceHolderMain_lblPermitNumber"
SEL_DETAIL_PARCELS = "#ctl00_PlaceHolderMain_PermitDetailList1_tbParcelList"

# Grid header links (id suffix -> label) the parser depends on.
EXPECTED_HEADERS = {
    "lnkDateHeader": "Opened Date",
    "lnkPermitNumberHeader": "Record Number",
    "lnkPermitTypeHeader": "Record Type",
    "lnkStatusHeader": "Status",
}

_DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
_SHOWING_RE = re.compile(r"Showing\s+(\d+)\s*-\s*(\d+)\s+of\s+(\d+)(\+?)")
_PARCEL_RE = re.compile(r"Parcel Number:\s*([\d\-]+)")
_ZIP_TAIL_RE = re.compile(r"(?:,?\s*(?:WA\s+)?(\d{5})(?:-\d{4})?)?(?:\s+United States)?\s*$",
                          re.IGNORECASE)
_CASE_RE = re.compile(r"^[A-Z]{2,6}\d{2}-\d{3,6}$")


class AccelaAccessWallError(RuntimeError):
    """The portal put a login, captcha or terms page in front of the records. Not retried."""


class AccelaFormatError(RuntimeError):
    """A page did not have the structure this adapter reads (canary)."""


class AccelaBudgetError(DateRangeTooLargeError):
    """The date range holds more cases than one run may look up. Not retried."""


_NOT_RETRYABLE = (AccelaAccessWallError, AccelaBudgetError)


@dataclass(frozen=True)
class GridRow:
    """One results-grid row. Deliberately has no description or notes field."""

    opened: date
    case_number: str
    detail_path: str
    record_type: str | None
    status: str | None
    #: The street line only; its ZIP is in ``zip``.
    address: str | None
    zip: str | None


@dataclass(frozen=True)
class ResultsPage:
    rows: list[GridRow]
    #: (first, last, total, total_is_lower_bound) from "Showing 1-10 of 100+", if printed.
    showing: tuple[int, int, int, bool] | None
    has_next: bool


@dataclass(frozen=True)
class CaseDetail:
    case_number: str
    parcel_numbers: list[str]
    address: str | None
    zip: str | None


@dataclass(frozen=True)
class PageSnapshot:
    url: str
    html: str


# ── Pure parsing ─────────────────────────────────────────────────────────────

def split_windows(start: date, end: date, days: int = WINDOW_DAYS) -> list[tuple[date, date]]:
    """Consecutive inclusive windows of at most ``days`` days covering start..end."""
    if end < start:
        return []
    out = []
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=days - 1), end)
        out.append((cur, stop))
        cur = stop + timedelta(days=1)
    return out


def detect_wall(snapshot: PageSnapshot) -> str | None:
    """Why this page is a login, captcha or terms wall, or None when it is not one.

    The normal pages link to Login.aspx and mention a disclaimer in the header, so only
    landing on the login page, a captcha widget, or a terms-acceptance control counts.
    """
    path = urlsplit(snapshot.url).path.lower()
    if path.endswith("/login.aspx"):
        return "login page"
    soup = BeautifulSoup(snapshot.html, "lxml")
    if soup.select_one(".g-recaptcha, .h-captcha, iframe[src*='recaptcha'], "
                       "iframe[src*='hcaptcha'], script[src*='recaptcha'], script[src*='hcaptcha']"):
        return "captcha"
    for box in soup.select("input[type='checkbox']"):
        ident = f"{box.get('id') or ''} {box.get('name') or ''}".lower()
        if any(word in ident for word in ("accept", "agree", "terms")):
            return "terms acceptance"
    return None


def ensure_no_wall(snapshot: PageSnapshot) -> None:
    reason = detect_wall(snapshot)
    if reason:
        raise AccelaAccessWallError(
            f"{KINGCO_ACCELA}: the portal showed a {reason} at {snapshot.url[:120]}; "
            f"stopping this source rather than working around it")


def _text(el) -> str | None:
    if el is None:
        return None
    value = " ".join(el.get_text(" ", strip=True).split())
    return value or None


def split_address(raw: str | None) -> tuple[str | None, str | None]:
    """(street, ZIP) from a portal address, or (None, None) when it has no street.

    The grid prints "7016 S LAKERIDGE DR, 98178", sometimes "..., WA 98024" or
    "..., 98051 United States", and "United States" alone when no address was entered.
    Unincorporated cases have no city, so none is invented.
    """
    value = " ".join((raw or "").split())
    if not value:
        return None, None
    m = _ZIP_TAIL_RE.search(value)
    street = value[:m.start()].strip(" ,") if m else value
    zipcode = m.group(1) if m else None
    if not street or street.lower() == "united states" or not any(c.isdigit() for c in street):
        return None, None
    return street, zipcode


def parse_results_page(html: str) -> ResultsPage:
    """The grid rows, "Showing" counts and whether a Next page exists.

    Raises AccelaFormatError when the page has neither the grid nor the no-results
    message, when the grid's expected headers are missing, when any grid row does not
    parse, or when the row count disagrees with the printed "Showing" range (the canaries
    for a portal layout change; a partly read page must never pass as complete).
    """
    soup = BeautifulSoup(html, "lxml")
    grid = soup.select_one(SEL_GRID)
    if grid is None:
        if "returned no results" in (_text(soup.select_one(SEL_NO_RESULTS)) or ""):
            return ResultsPage(rows=[], showing=None, has_next=False)
        raise AccelaFormatError(
            f"{KINGCO_ACCELA}: results page has neither the record grid nor the no-results message")

    header = grid.select_one("tr.ACA_TabRow_Header")
    labels = {}
    if header is not None:
        for link in header.select("a[id]"):
            labels[link["id"].rsplit("_", 1)[-1]] = _text(link)
    missing = [k for k, label in EXPECTED_HEADERS.items() if labels.get(k) != label]
    if missing:
        raise AccelaFormatError(f"{KINGCO_ACCELA}: record grid headers changed (missing {missing})")

    # Every odd/even grid row is one case (verified on every saved results page), so a
    # row that does not parse is a layout change, never a row to skip: skipping it would
    # ship this source as a success with cases missing.
    raw_rows = grid.select("tr.ACA_TabRow_Odd, tr.ACA_TabRow_Even")
    rows: list[GridRow] = []
    unreadable = 0
    for tr in raw_rows:
        def cell(suffix: str, _tr=tr):
            return _tr.select_one(f"[id$='_{suffix}']")

        link = cell("hlPermitNumber")
        case = _text(link)
        date_match = _DATE_RE.match(_text(cell("lblUpdatedTime")) or "")
        href = link.get("href") if link is not None else None
        if not case or not _CASE_RE.match(case) or not date_match or not href:
            unreadable += 1
            continue
        try:
            opened = date(int(date_match.group(3)), int(date_match.group(1)), int(date_match.group(2)))
        except ValueError:
            unreadable += 1
            continue
        street, zipcode = split_address(_text(cell("lblPermitAddress")))
        rows.append(GridRow(
            opened=opened,
            case_number=case,
            detail_path=href,
            record_type=(_text(cell("lblType")) or "")[:LABEL_MAX] or None,
            status=(_text(cell("lblStatus")) or "")[:LABEL_MAX] or None,
            address=street,
            zip=zipcode,
        ))
    if unreadable:
        raise AccelaFormatError(
            f"{KINGCO_ACCELA}: {unreadable} of {len(raw_rows)} grid rows had no readable case "
            f"number and date")

    showing = None
    m = _SHOWING_RE.search(grid.get_text(" ", strip=True))
    if m:
        showing = (int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) == "+")
    if rows and (showing is None or len(rows) != showing[1] - showing[0] + 1):
        raise AccelaFormatError(
            f"{KINGCO_ACCELA}: the grid lists {len(rows)} rows but its range reads "
            f"{'nothing' if showing is None else f'{showing[0]}-{showing[1]}'}; "
            f"cannot prove the page is complete")
    has_next = any(
        (_text(a) or "").startswith("Next") and "__doPostBack" in (a.get("href") or "")
        for a in grid.select("tr.ACA_Table_Pages a"))
    if showing and not showing[3] and showing[1] >= showing[2]:
        has_next = False
    return ResultsPage(rows=rows, showing=showing, has_next=has_next)


def parse_case_detail(html: str, expected_case: str) -> CaseDetail:
    """The parcel number(s) and work-location address from a case detail page.

    Raises AccelaFormatError when the page is not the expected case's detail page.
    """
    soup = BeautifulSoup(html, "lxml")
    case = _text(soup.select_one(SEL_DETAIL_CASE))
    if case != expected_case:
        raise AccelaFormatError(
            f"{KINGCO_ACCELA}: detail page shows case {case!r}, expected {expected_case!r}")
    pins: list[str] = []
    parcels = soup.select_one(SEL_DETAIL_PARCELS)
    if parcels is not None:
        for raw in _PARCEL_RE.findall(parcels.get_text(" ", strip=True)):
            pin = normalize_king_pin(raw)
            if pin and pin not in pins:
                pins.append(pin)
    street = zipcode = None
    location = soup.select_one("#divWorkLocationInfo")
    if location is not None:
        parts = [p for p in (" ".join(s.split()) for s in location.stripped_strings) if p and p != "*"]
        street, zipcode = split_address(", ".join(parts))
    return CaseDetail(case_number=case, parcel_numbers=pins, address=street, zip=zipcode)


def detail_url(path: str) -> str:
    """Absolute detail URL on the portal host; anything pointing elsewhere is refused."""
    url = urljoin(SEARCH_URL, path)
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (parts.scheme != "https" or parts.hostname != _HOST or port not in (None, 443)
            or parts.username is not None or parts.password is not None
            or not parts.path.startswith("/KINGCO/Cap/CapDetail.aspx")):
        raise AccelaFormatError(f"{KINGCO_ACCELA}: unexpected case detail link {path[:120]!r}")
    return url


def is_detail_page(snapshot: PageSnapshot) -> bool:
    return urlsplit(snapshot.url).path.lower().endswith("/cap/capdetail.aspx")


def single_result_row(snapshot: PageSnapshot, day: date) -> GridRow:
    """The grid row for a one-day search the portal answered with the case's detail page.

    The detail page prints no opened date; the search covered only ``day``, so that is it.
    """
    soup = BeautifulSoup(snapshot.html, "lxml")
    case = _text(soup.select_one(SEL_DETAIL_CASE))
    if not case or not _CASE_RE.match(case):
        raise AccelaFormatError(f"{KINGCO_ACCELA}: single-result detail page has no case number")
    parts = urlsplit(snapshot.url)
    path = f"{parts.path}?{parts.query}" if parts.query else parts.path
    detail = parse_case_detail(snapshot.html, case)
    return GridRow(
        opened=day,
        case_number=case,
        detail_path=path,
        record_type=(_text(soup.select_one("#ctl00_PlaceHolderMain_lblPermitType")) or "")[:LABEL_MAX] or None,
        status=(_text(soup.select_one("#ctl00_PlaceHolderMain_lblRecordStatus")) or "")[:LABEL_MAX] or None,
        address=detail.address,
        zip=detail.zip,
    )


def build_record(row: GridRow, detail: CaseDetail) -> ScrapedRecord:
    parcel_id = detail.parcel_numbers[0] if len(detail.parcel_numbers) == 1 else None
    # Street and ZIP come from the same printed address, never one from each page.
    street, zipcode = (row.address, row.zip) if row.address else (detail.address, detail.zip)
    record = ScrapedRecord(
        date_recorded=row.opened.strftime("%m/%d/%Y"),
        party_name=None,
        legal_description=row.case_number,
        parcel_id=parcel_id,
        property_address=street,
        property_zip=zipcode,
        raw_html_hash=hashlib.sha256(f"{KINGCO_ACCELA}|{row.case_number}".encode()).hexdigest()[:32],
    )
    record.enrichment_data = {
        "source": KINGCO_ACCELA,
        "case_number": row.case_number,
        "status": row.status,
        "violation_category": None,
        "case_type": row.record_type,
        "opened_date": row.opened.strftime("%m/%d/%Y"),
        "source_parcel_numbers": list(detail.parcel_numbers),
    }
    return record


def check_paging_budget(page: ResultsPage, deadline: float) -> None:
    """Stop a search before its next page when the run cannot use what paging would read.

    Checked inside one search, between pages, so a large or slow window cannot spend the
    worker's scrape timeout (and with it the other jurisdictions) before control returns
    to the source: a window already past MAX_DETAIL_PAGES rows, or one whose exact total
    is over it, is too many cases for one run; past ``deadline`` the time budget is spent.
    """
    if page.showing is not None:
        _first, last, total, lower_bound = page.showing
        if last >= MAX_DETAIL_PAGES or (not lower_bound and total > MAX_DETAIL_PAGES):
            raise AccelaBudgetError(
                f"{KINGCO_ACCELA}: a search window lists {total}{'+' if lower_bound else ''} cases, "
                f"more than the {MAX_DETAIL_PAGES} one run can look up; run a shorter date range")
    if time.monotonic() >= deadline:
        raise AccelaBudgetError(
            f"{KINGCO_ACCELA}: stopped paging after the {TIME_BUDGET_SECONDS}s time budget; "
            f"run a shorter date range")


# ── Browser ──────────────────────────────────────────────────────────────────

class AccelaPortal(BridgeScraper):
    """The Playwright session: one fresh context per search, paced navigations."""

    def __init__(self) -> None:
        super().__init__()
        self._last_action = 0.0
        #: time.monotonic() after which paging stops; the source sets its run deadline.
        self.deadline = float("inf")

    async def _pace(self) -> None:
        wait = self._last_action + PACE_SECONDS - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_action = time.monotonic()

    async def _snapshot(self) -> PageSnapshot:
        snap = PageSnapshot(url=self.page.url, html=await self.page.content())
        ensure_no_wall(snap)
        if "/error.aspx" in urlsplit(snap.url).path.lower():
            raise RuntimeError(f"{KINGCO_ACCELA}: the portal returned its error page")
        return snap

    async def _postback(self, action) -> None:
        timeout = settings.DEFAULT_TIMEOUT * 1000
        await self._pace()
        async with self.page.expect_response(
                lambda r: r.request.method == "POST" and urlsplit(r.url).hostname == _HOST,
                timeout=timeout):
            await action()
        await self.page.wait_for_load_state("networkidle", timeout=timeout)

    async def search(self, start: date, end: date) -> list[PageSnapshot]:
        """Every results page for cases opened start..end, in a new server session."""
        timeout = settings.DEFAULT_TIMEOUT * 1000
        await self.reset_context()
        await self._pace()
        await self.safe_goto(SEARCH_URL, wait_until="networkidle", timeout_ms=timeout)
        form = await self._snapshot()
        soup = BeautifulSoup(form.html, "lxml")
        if (soup.select_one(f"{SEL_RECORD_TYPE} option[value='{RECORD_TYPE_VALUE}']") is None
                or soup.select_one(SEL_START_DATE) is None or soup.select_one(SEL_END_DATE) is None):
            raise AccelaFormatError(f"{KINGCO_ACCELA}: the search form changed")

        await self._pace()
        await self.page.select_option(SEL_RECORD_TYPE, RECORD_TYPE_VALUE)
        await self.page.wait_for_load_state("networkidle", timeout=timeout)
        for selector, value in ((SEL_START_DATE, start), (SEL_END_DATE, end)):
            box = self.page.locator(selector)
            await box.click()
            await box.press("Control+a")
            await box.press("Delete")
            await box.type(value.strftime("%m/%d/%Y"), delay=40)
            await box.press("Tab")
        typed = (await self.page.input_value(SEL_START_DATE), await self.page.input_value(SEL_END_DATE),
                 await self.page.input_value(SEL_RECORD_TYPE))
        if typed != (start.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y"), RECORD_TYPE_VALUE):
            raise RuntimeError(f"{KINGCO_ACCELA}: search form did not take the criteria ({typed})")

        await self._postback(lambda: self.page.click(SEL_SEARCH))
        # One match skips the grid and lands on that case's detail page.
        await self.page.wait_for_selector(f"{SEL_GRID}, {SEL_NO_RESULTS}, {SEL_DETAIL_CASE}",
                                          timeout=timeout)
        pages = [await self._snapshot()]
        if is_detail_page(pages[0]):
            return pages
        for _ in range(MAX_PAGES):
            current = parse_results_page(pages[-1].html)
            if not current.has_next:
                return pages
            check_paging_budget(current, self.deadline)
            await self._postback(lambda: self.page.locator(SEL_NEXT).first.click())
            # The postback settles before the grid is swapped in (verified live: a
            # snapshot taken at networkidle was still the previous page), so wait for
            # the "Showing" range to move past the page we already have.
            await self.page.wait_for_function(
                """([grid, last]) => {
                    const g = document.querySelector(grid);
                    const m = g && g.innerText.match(/Showing\\s+(\\d+)\\s*-\\s*\\d+/);
                    return m !== null && Number(m[1]) > last;
                }""",
                arg=[SEL_GRID, current.showing[1] if current.showing else 0],
                timeout=timeout)
            pages.append(await self._snapshot())
        raise AccelaFormatError(f"{KINGCO_ACCELA}: hit the {MAX_PAGES}-page guard")

    async def case_detail(self, path: str) -> PageSnapshot:
        timeout = settings.DEFAULT_TIMEOUT * 1000
        await self._pace()
        await self.safe_goto(detail_url(path), wait_until="domcontentloaded", timeout_ms=timeout)
        await self.page.wait_for_selector(SEL_DETAIL_CASE, timeout=timeout)
        return await self._snapshot()


# ── Source ───────────────────────────────────────────────────────────────────

class KingCountyAccelaSource(CodeViolationSource):
    key = KINGCO_ACCELA
    jurisdiction = "unincorporated King County"

    #: The portal session class; a context manager with search() and case_detail().
    portal_class = AccelaPortal

    def __init__(self) -> None:
        super().__init__()
        self._deadline = float("inf")

    async def _retrying(self, what: str, call):
        """Run one portal step with bounded retries; walls and budget errors are final."""
        last_exc: Exception | None = None
        for attempt in range(1, settings.MAX_RETRIES + 1):
            if time.monotonic() >= self._deadline:
                raise AccelaBudgetError(
                    f"{self.key}: stopped at {what} after the {TIME_BUDGET_SECONDS}s time budget; "
                    f"run a shorter date range")
            try:
                return await call()
            except _NOT_RETRYABLE:
                raise
            except Exception as exc:
                if type(exc).__name__ in ("SoftTimeLimitExceeded", "TimeLimitExceeded"):
                    raise
                last_exc = exc
                if attempt >= settings.MAX_RETRIES:
                    break
                wait = (2 ** attempt) + random.uniform(0, 0.5)
                _logger.warning("%s failed (attempt %d/%d): %s; retrying in %.1fs",
                                what, attempt, settings.MAX_RETRIES, str(exc)[:160], wait)
                await asyncio.sleep(wait)
        raise RuntimeError(
            f"{what} failed after {settings.MAX_RETRIES} attempt(s); aborting this source rather "
            f"than returning a truncated result: {str(last_exc)[:160]}") from last_exc

    async def fetch(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        start = datetime.strptime(date_from, "%m/%d/%Y").date()
        end = datetime.strptime(date_to, "%m/%d/%Y").date()
        _logger.info("King County Accela code enforcement %s to %s", date_from, date_to)
        self._deadline = time.monotonic() + TIME_BUDGET_SECONDS
        async with self.portal_class() as portal:
            portal.deadline = self._deadline
            rows = await self._list_cases(portal, start, end)
            records = []
            for n, row in enumerate(rows, start=1):
                detail = await self._retrying(
                    f"{self.key} case {row.case_number}",
                    lambda row=row: self._case_detail(portal, row))
                records.append(build_record(row, detail))
                self._progress(n, len(records))
        _logger.info("King County Accela code enforcement: %d cases, %d with a parcel",
                     len(records), sum(1 for r in records if r.parcel_id))
        return records

    async def _list_cases(self, portal, start: date, end: date) -> list[GridRow]:
        rows: dict[str, GridRow] = {}
        pending = split_windows(start, end)
        while pending:
            w_start, w_end = pending.pop(0)
            found = await self._retrying(
                f"{self.key} search {w_start:%m/%d/%Y}-{w_end:%m/%d/%Y}",
                lambda a=w_start, b=w_end: self._search_window(portal, a, b))
            if found is None:
                # Exactly one case in a multi-day window: the portal showed its detail page,
                # which has no opened date. Search the window's days one by one instead.
                pending[:0] = split_windows(w_start, w_end, days=1)
                continue
            for row in found:
                rows.setdefault(row.case_number, row)
            if len(rows) > MAX_DETAIL_PAGES:
                raise AccelaBudgetError(
                    f"{self.key}: more than {MAX_DETAIL_PAGES} cases opened {start:%m/%d/%Y} to "
                    f"{end:%m/%d/%Y}, more than one run can look up; run a shorter date range")
        return list(rows.values())

    async def _search_window(self, portal, start: date, end: date) -> list[GridRow] | None:
        """The window's rows, or None when a multi-day search landed on a single case."""
        return rows_from_search(await portal.search(start, end), start, end)

    async def _case_detail(self, portal, row: GridRow) -> CaseDetail:
        snap = await portal.case_detail(row.detail_path)
        ensure_no_wall(snap)
        return parse_case_detail(snap.html, row.case_number)


def rows_from_search(pages: Iterable[PageSnapshot], start: date, end: date) -> list[GridRow] | None:
    """Grid rows opened start..end from one search's pages, checking the paging advanced.

    A search the portal answered with a case detail page (one match) yields that case when
    the window is one day, and None otherwise (the opened date is not on the page).
    """
    pages = list(pages)
    for snap in pages:
        ensure_no_wall(snap)
    if pages and is_detail_page(pages[0]):
        return [single_result_row(pages[0], start)] if start == end else None
    out: list[GridRow] = []
    expected_first = 1
    page = None
    for snap in pages:
        page = parse_results_page(snap.html)
        if page.showing is not None:
            if page.showing[0] != expected_first:
                raise AccelaFormatError(
                    f"{KINGCO_ACCELA}: results page starts at row {page.showing[0]}, expected "
                    f"{expected_first}; paging did not advance")
            expected_first = page.showing[1] + 1
        out.extend(r for r in page.rows if start <= r.opened <= end)
    if page is not None and page.showing is not None and (page.showing[3] or page.showing[1] < page.showing[2]):
        raise AccelaFormatError(
            f"{KINGCO_ACCELA}: the last results page shows rows {page.showing[0]}-{page.showing[1]} "
            f"of {page.showing[2]}{'+' if page.showing[3] else ''}; results were not read to the end")
    return out
