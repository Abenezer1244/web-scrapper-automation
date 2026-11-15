"""Ferry probate/divorce from the State Archives court index (scrapers/ferry_wa_court_index).

The index reader is replaced by a stand-in that answers from the real (redacted)
Ferry pages in tests/fixtures/wa_archives_ferry, keyed by year or case number.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.scrapers import ferry_wa_court_index as court
from src.scrapers.reliability import ScraperExecutionError
from src.scrapers.sources import wa_digital_archives as da

FIX = Path(__file__).parent / "fixtures" / "wa_archives_ferry"


def _rows(name: str) -> list[da.DocketRow]:
    return da.parse_results_page((FIX / name).read_text(encoding="utf-8"), "ferry")[1]


CASES = {c: _rows(f"case_{c}.html") for c in
         ("25-4-00006-10", "25-4-00012-10", "25-4-00034-10", "25-3-00001-10")}
# A 2025 "year" made of the four real case histories: four openings, Jan-Oct 2025.
YEAR_2025 = [r for rows in CASES.values() for r in rows]


class FakeIndex:
    searches: list = []

    def __init__(self, page, county):
        assert county == "ferry"

    async def search(self, *, year=None, case_no=None):
        FakeIndex.searches.append(year or case_no)
        if case_no:
            return CASES.get(case_no, [])
        return {2025: YEAR_2025, 2026: _rows("year_2026_p5_of_5.html")}.get(year, [])


@pytest.fixture(autouse=True)
def fake_index(monkeypatch):
    FakeIndex.searches = []
    monkeypatch.setattr(da, "ArchivesIndex", FakeIndex)
    monkeypatch.setattr(court, "validate_scraping_target", lambda url, resolve: None)


def _run(cls, start: str, end: str):
    import asyncio

    scraper = cls()
    return asyncio.run(scraper.scrape(start, end))


def test_openings_are_filtered_by_type_and_window():
    assert court.openings_in_window(YEAR_2025, "4", date(2025, 1, 1), date(2025, 3, 1)) == \
        ["25-4-00006-10", "25-4-00012-10"]
    assert court.openings_in_window(YEAR_2025, "3", date(2025, 1, 1), date(2025, 12, 31)) == \
        ["25-3-00001-10"]
    assert court.openings_in_window(YEAR_2025, "4", date(2025, 11, 1), date(2025, 12, 31)) == []


def test_a_window_past_the_index_reach_is_not_covered():
    assert court.coverage_error(YEAR_2025, date(2025, 10, 20), "probate") is None
    err = court.coverage_error(YEAR_2025, date(2025, 10, 21), "probate")
    assert isinstance(err, ScraperExecutionError) and "2025-10-20" in str(err)
    assert court.coverage_error([], date(2025, 1, 1), "probate") is not None


def test_a_2026_window_fails_because_the_index_has_no_2026_filings():
    with pytest.raises(ScraperExecutionError, match="not covered"):
        _run(court.FerryWAProbateScraper, "01/01/2026", "03/31/2026")


def test_an_open_estate_in_the_window_is_held_never_delivered():
    with pytest.raises(ScraperExecutionError, match="1 probate filing"):
        _run(court.FerryWAProbateScraper, "01/01/2025", "02/28/2025")
    # Both openings were looked up; only the open estate qualifies (the other is closed).
    assert "25-4-00006-10" in FakeIndex.searches and "25-4-00012-10" in FakeIndex.searches


def test_a_window_with_no_estate_is_still_not_a_verified_zero():
    with pytest.raises(ScraperExecutionError, match="not a verified zero"):
        _run(court.FerryWAProbateScraper, "10/01/2025", "10/20/2025")  # a guardianship only


def test_a_dissolution_in_the_window_is_held_never_delivered():
    with pytest.raises(ScraperExecutionError, match="1 divorce filing"):
        _run(court.FerryWADivorceScraper, "01/01/2025", "10/20/2025")


def test_a_closed_estate_alone_is_not_a_lead_and_not_a_verified_zero():
    with pytest.raises(ScraperExecutionError, match="no probate filing found"):
        _run(court.FerryWAProbateScraper, "02/27/2025", "10/20/2025")


def test_the_court_modules_are_allowlisted():
    from src.scrapers.registry import _ALLOWED_SCRAPER_MODULES

    assert "src.scrapers.ferry_wa_court_index" in _ALLOWED_SCRAPER_MODULES


@pytest.mark.parametrize("history", [
    [],                                                    # the case search came back empty
    CASES["25-4-00012-10"],                                # another case
    [r for r in CASES["25-4-00006-10"] if r.code != da.OPENING_CODE],  # opening missing
], ids=["empty", "other case", "no opening"])
def test_a_case_history_that_does_not_match_its_opening_is_drift(history, monkeypatch):
    real = dict(CASES)
    monkeypatch.setitem(CASES, "25-4-00006-10", history)
    with pytest.raises(da.IndexDriftError):
        _run(court.FerryWAProbateScraper, "01/01/2025", "01/31/2025")
    assert real  # the fixture map is restored by monkeypatch
