"""Pierce ARMS: a results page holding 1-3 records must still parse.

Background (Test 11, 2026-09-04). Batch "Test 11" showed "Completed with errors"
because its pierce/pre_foreclosure child failed three times. `_extract_records`
picked the ARMS results grid with `if len(data_rows) < 5: continue`. The grid is
one header <tr> plus one <tr> per record, so a page holding 1-3 records was
skipped, `data_table` came back None, the "N records found" marker was non-zero,
and it raised TransientScrapeError -> 2 retries -> the whole job failed.

Two live shapes hit it:
  * a whole search returning 1-3 records (pierce/divorce 08/24-08/28 = 1 record);
  * the LAST page of a multi-page search when the remainder is 1-3 — the Test 11
    range was 228 records over 10 pages of 25, so page 10 held exactly 3 rows and
    the job died after nine good pages (`page_current=9, page_total=10` in the
    prod row).

Fixtures reproduce the live page captured 2026-09-04: the grid carries no id or
class and shares the page with a criteria table, "Showing Records" and pager
tables, and a wide image table — several of which have MORE rows than a small
grid, which is why a row COUNT can never identify it.

Pure BeautifulSoup, no browser and no network.
"""
import pytest
from bs4 import BeautifulSoup

from src.scrapers.pierce_wa_probate import (
    PierceWADivorceScraper,
    PierceWAPreForeclosureScraper,
)
from src.scrapers.reliability import TransientScrapeError

# ── live page chrome: every one of these outranks a 3-row grid on row count ──
_CRITERIA = "<table id='Table1'>" + "".join(
    f"<tr><td>Criteria:Date Filed {i}</td></tr>" for i in range(26)
) + "</table>"
_SHOWING = "<table id='Table1'><tr><th>h</th></tr><tr><td>Showing Records 1-3</td></tr>" \
           "<tr><td>x</td></tr></table>"
_PAGER = "<table><tr><th>h</th></tr><tr><td>Page 1</td><td>of 1</td><td>&gt;</td>" \
         "<td>&gt;&gt;</td></tr><tr><td>x</td><td></td><td></td><td></td></tr></table>"
_IMAGE = "<table><tr><th>h</th></tr>" + "".join(
    "<tr>" + "<td>#ImageInstrument</td>" * 12 + "</tr>" for _ in range(10)
) + "</table>"
_CHROME = _CRITERIA + _SHOWING + _PAGER + _IMAGE


def _grid_row(n: int, inst: str, filed: str = "08/24/2026",
              doc_type: str = "TRUSTEE SALE") -> str:
    return (
        f"<tr><td>{n}</td><td>View</td><td></td>"
        f"<td><a href='#'>{inst}</a></td><td>{inst}</td><td></td><td></td>"
        f"<td>{filed}</td><td>{filed}</td>"
        f"<td>{doc_type}</td><td>{doc_type}</td>"
        f"<td>[R] QUALITY LOAN SERVICE CORP (+) [E] SMITH JANE</td><td>R</td><td></td>"
        f"<td>BELMONT DIV 1 LT 7 (+)</td><td></td><td>V</td><td>Y</td></tr>"
    )


def _page(n_records: int, *, wrap: bool = False, chrome: str = _CHROME) -> BeautifulSoup:
    """A full ARMS results page whose grid holds `n_records` rows."""
    grid = "<table><tr><th>#</th><th>Doc</th></tr>" + "".join(
        _grid_row(i + 1, f"20260824{i:04d}") for i in range(n_records)
    ) + "</table>"
    if wrap:  # ASP.NET layout table that ENCLOSES the grid
        grid = f"<table><tr><td>{grid}</td></tr></table>"
    return BeautifulSoup(f"<html><body>{chrome}{grid}</body></html>", "html.parser")


def _scraper(marker: str, cls=PierceWAPreForeclosureScraper):
    s = cls()
    s._record_count = marker
    return s


# ── the Test 11 mechanism ────────────────────────────────────────────────────
@pytest.mark.parametrize("n_records", [1, 2, 3, 4])
def test_small_result_page_is_parsed_not_treated_as_missing(n_records):
    """1-3 rows is a real page, not a missing table. This is the Test 11 bug."""
    recs = _scraper("228")._extract_records(_page(n_records))
    assert len(recs) == n_records


def test_last_page_of_a_multipage_search_with_three_rows():
    """Test 11 exactly: 228 records / 10 pages of 25 -> page 10 holds 3 rows."""
    assert 228 % 25 == 3
    recs = _scraper("228")._extract_records(_page(3))
    assert [r.enrichment_data["instrument_number"] for r in recs] == [
        "202608240000", "202608240001", "202608240002",
    ]


def test_single_record_search_parses():
    """pierce/divorce 08/24-08/28 returned exactly 1 record and used to fail."""
    recs = _scraper("1", PierceWADivorceScraper)._extract_records(_page(1))
    assert len(recs) == 1


# ── no regression on the shapes that already worked ──────────────────────────
@pytest.mark.parametrize("n_records", [5, 9, 25])
def test_full_pages_still_parse(n_records):
    assert len(_scraper(str(n_records))._extract_records(_page(n_records))) == n_records


def test_genuine_empty_day_returns_empty_and_does_not_raise():
    soup = BeautifulSoup(f"<html><body>{_CHROME}</body></html>", "html.parser")
    assert _scraper("0")._extract_records(soup) == []


def test_missing_grid_with_nonzero_marker_still_fails_loud():
    """A blocked / never-rendered page must NEVER be scored as a healthy 0."""
    soup = BeautifulSoup(f"<html><body>{_CHROME}</body></html>", "html.parser")
    with pytest.raises(TransientScrapeError):
        _scraper("17")._extract_records(soup)


# ── table selection stays honest (Codex) ─────────────────────────────────────
def test_wrapper_table_is_not_picked_over_the_grid_it_encloses():
    """find_all("tr") is recursive, so an enclosing layout table reports the
    grid's own rows. Row scoping must still resolve to the real grid."""
    recs = _scraper("3")._extract_records(_page(3, wrap=True))
    assert len(recs) == 3


def test_numeric_wide_chrome_row_without_a_date_is_not_mistaken_for_the_grid():
    """A wide, numerically-led chrome row is rejected by the date signature."""
    decoy = "<table><tr><th>h</th></tr><tr>" + "<td>1</td>" * 12 + "</tr></table>"
    soup = BeautifulSoup(f"<html><body>{decoy}</body></html>", "html.parser")
    with pytest.raises(TransientScrapeError):
        _scraper("9")._extract_records(soup)


def test_dated_chrome_row_without_an_instrument_still_fails_loud():
    """The dangerous shape (Codex P1): a blocked page carrying a wide,
    numerically-led, DATED status row. If it were accepted as the grid, every row
    would fail to map and _extract_records would return [] — scoring a blocked
    page as a healthy zero, which is exactly what the raise exists to prevent.
    The instrument-number requirement keeps it a hard failure."""
    decoy = (
        "<table><tr><th>h</th></tr><tr><td>1</td><td>Session expired</td>"
        "<td>09/04/2026</td>" + "<td>-</td>" * 9 + "</tr></table>"
    )
    soup = BeautifulSoup(f"<html><body>{decoy}</body></html>", "html.parser")
    with pytest.raises(TransientScrapeError):
        _scraper("9")._extract_records(soup)


def test_dated_chrome_row_with_an_unrelated_10_digit_id_still_fails_loud():
    """Codex round 2: a bare 10-digit id (a case/request number) is NOT an ARMS
    instrument — _map_row's no-link fallback only accepts "20" + 10 digits. The
    signature resolves the instrument through that same helper, so such a row
    cannot pass as the grid and silently return []."""
    decoy = (
        "<table><tr><th>h</th></tr><tr><td>1</td><td>Request 4177060700</td>"
        "<td>09/04/2026</td>" + "<td>-</td>" * 9 + "</tr></table>"
    )
    soup = BeautifulSoup(f"<html><body>{decoy}</body></html>", "html.parser")
    with pytest.raises(TransientScrapeError):
        _scraper("9")._extract_records(soup)


def test_a_grid_whose_rows_are_all_filtered_still_returns_empty_not_raises():
    """A real grid whose every row is dropped by a PRODUCT filter (pre_foreclosure
    drops rows with no natural-person party) is found, parsed, and legitimately
    yields nothing. That must not be confused with a missing table."""
    corporate = _grid_row(1, "202608240099").replace(
        "[R] QUALITY LOAN SERVICE CORP (+) [E] SMITH JANE",
        "[R] QUALITY LOAN SERVICE CORP (+) [E] 1436 E 31ST ST LLC",
    )
    grid = f"<table><tr><th>#</th></tr>{corporate}</table>"
    soup = BeautifulSoup(f"<html><body>{_CHROME}{grid}</body></html>", "html.parser")
    assert _scraper("1")._extract_records(soup) == []


def test_header_row_is_never_emitted_as_a_record():
    recs = _scraper("3")._extract_records(_page(3))
    assert all(r.enrichment_data.get("instrument_number") for r in recs)
