"""Ferry County (WA) — probate and divorce filings from the State Archives court index.

Ferry's Auditor has no online recorder index, so the only public list of new estates
and dissolutions is the Superior Court docket index the State Archives publishes
(sources/wa_digital_archives). A case counts as filed in the window when its opening
entry (CICS, the case information cover sheet) is dated inside it; its whole docket
history then decides what it is (classify_probate / classify_divorce).

CONNECTORS STAY INACTIVE. GR 31(g)(4) bars commercial solicitation of people named in
court records distributed in bulk, and nothing here ties a case to a Ferry parcel: a
name alone is not ownership. So no lead is ever DELIVERED from this module. A window
that holds qualifying filings fails the job with the held count (never a silent
zero); a window with none, inside the index's reach, is a verified zero.

NEVER A FALSE ZERO: the index trails the clerk (newest Ferry opening on 2026-11-15:
November 2025). A window ending after the newest opening the index shows is not
covered and fails the job instead of answering.
"""
from __future__ import annotations

from datetime import date, datetime

from src.api.middleware.security import validate_scraping_target
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.scrapers.reliability import ScraperExecutionError
from src.scrapers.sources import wa_digital_archives as da
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.ferry_wa_court_index")
_COUNTY = "ferry"


def _mdy(value: str) -> date:
    return datetime.strptime(value, "%m/%d/%Y").date()


def openings_in_window(rows: list[da.DocketRow], case_type: str, start: date, end: date) -> list[str]:
    """Case numbers of the given type whose opening entry falls in [start, end]."""
    return sorted({r.case_no for r in rows if r.code == da.OPENING_CODE
                   and r.case_type == case_type and start <= r.docket_date <= end})


def coverage_error(rows: list[da.DocketRow], end: date, record_type: str) -> ScraperExecutionError | None:
    """The error to raise when the index does not reach ``end``; None when it does."""
    reach = da.newest_opening(rows)
    if reach is not None and end <= reach:
        return None
    seen = reach.isoformat() if reach else "none in the searched years"
    return ScraperExecutionError(
        _COUNTY, "wa_digital_archives",
        f"the court index's newest filing is {seen}; the window ends {end.isoformat()} "
        "and is not covered yet", record_type=record_type)


def check_history(history: list[da.DocketRow], opening: da.DocketRow) -> None:
    """A case's history must be that case, and hold the opening the year search showed;
    anything else is drift, never a quiet "not a probate" (Codex)."""
    if not history or {r.case_no for r in history} != {opening.case_no}:
        raise da.IndexDriftError(_COUNTY, "wa_digital_archives",
                                 "a case search answered with no rows or another case")
    if not any(r.code == da.OPENING_CODE and r.docket_date == opening.docket_date
               for r in history):
        raise da.IndexDriftError(_COUNTY, "wa_digital_archives",
                                 "a case history without the opening the year search showed")


class _FerryCourtIndexScraper(BridgeScraper):
    RECORD_TYPE = ""
    CASE_TYPE = ""

    def keep(self, case: da.CourtCase) -> bool:
        return True

    def classify(self, history: list[da.DocketRow]) -> da.CourtCase | None:
        raise NotImplementedError

    def __init__(self, record_type: str | None = None) -> None:
        super().__init__()

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        start, end = _mdy(date_from), _mdy(date_to)
        validate_scraping_target(da.ORIGIN, resolve=True)
        index = da.ArchivesIndex(self.page, _COUNTY)
        self.report_stage("searching")
        rows: list[da.DocketRow] = []
        for year in range(start.year, end.year + 1):
            rows += await index.search(year=year)
        error = coverage_error(rows, end, self.RECORD_TYPE)
        if error is not None:
            raise error
        cases = []
        opened = {r.case_no: r for r in rows if r.code == da.OPENING_CODE}
        for case_no in openings_in_window(rows, self.CASE_TYPE, start, end):
            history = await index.search(case_no=case_no)
            check_history(history, opened[case_no])
            case = self.classify(history)
            if case is not None and self.keep(case):
                cases.append(case)
        _logger.info("ferry %s: %d docket rows, %d qualifying filing(s), all held "
                     "(no court-to-parcel match)", self.RECORD_TYPE, len(rows), len(cases))
        if cases:
            raise ScraperExecutionError(
                _COUNTY, "property_match",
                f"{len(cases)} {self.RECORD_TYPE} filing(s) in the window, none tied to a "
                "Ferry parcel (no court-to-parcel matcher; GR 31 review pending): held, "
                "not delivered", record_type=self.RECORD_TYPE)
        return []


class FerryWAProbateScraper(_FerryCourtIndexScraper):
    """Open estates (type 4, "ESTATE OF"), guardianships and closed estates excluded."""

    RECORD_TYPE = "probate"
    CASE_TYPE = "4"

    def classify(self, history):
        return da.classify_probate(history)

    def keep(self, case):
        return case.status == "open"


class FerryWADivorceScraper(_FerryCourtIndexScraper):
    """Dissolutions of marriage (type 3 with a docketed petition for dissolution)."""

    RECORD_TYPE = "divorce"
    CASE_TYPE = "3"

    def classify(self, history):
        return da.classify_divorce(history)
