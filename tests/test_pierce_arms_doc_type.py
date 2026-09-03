"""Pierce ARMS grid rows keep their REAL recorded document type (pre_foreclosure).

Background (Test 2 audit, 2026-09-02): the pre_foreclosure search checks four
ARMS document-type boxes (NOTICE OF DEFAULT / NOTICE OF FORECLOSURE / LIS PENDENS /
TRUSTEE SALE) and the results grid prints the exact type on every row, but the
scraper stamped a single "PRE-FORECLOSURE" label on all of them. Only a TRUSTEE
SALE can ever carry an auction date / default amount (matched later from the
newspaper NTS cache), so users could not tell a legitimately-blank Notice of
Default from a Trustee Sale that has simply not been published yet.

The fixture rows below reproduce the live grid cell layout captured on
2026-09-02 (instrument link, filed date twice, doc type twice, [R]/[E] name cell,
legal). No browser / network: `_map_row` is pure BeautifulSoup parsing.
"""
from bs4 import BeautifulSoup

from src.scrapers.pierce_wa_probate import (
    PierceWAARMSScraper,
    PierceWAPreForeclosureScraper,
    PierceWAProbateScraper,
)


def _grid_cells(doc_type: str, name_cell: str, legal: str = "PALMER LAKE LT 28 BLK 5 (+)",
                inst: str = "202608210171", filed: str = "08/21/2026") -> list:
    """One ARMS results-grid row as the scraper sees it (>= 9 <td>, first is the row #)."""
    html = (
        "<table><tr>"
        f"<td>1</td><td></td><td></td>"
        f"<td><a href='#'>{inst}</a></td><td>{inst}</td><td></td><td></td>"
        f"<td>{filed}</td><td>{filed}</td>"
        f"<td>{doc_type}</td><td>{doc_type}</td>"
        f"<td>{name_cell}</td><td>R</td><td></td>"
        f"<td>{legal}</td><td></td><td>V</td><td>Y</td>"
        "</tr></table>"
    )
    return BeautifulSoup(html, "html.parser").find_all("td")


def _pre_fc_row(doc_type: str, name_cell: str = "[R] STUART PAUL ALAN (+) [E] SEIM TERRY", **kw):
    return PierceWAPreForeclosureScraper()._map_row(_grid_cells(doc_type, name_cell, **kw))


def test_lis_pendens_row_keeps_its_real_doc_type():
    rec = _pre_fc_row("LIS PENDENS")
    assert rec is not None
    assert rec.doc_type == "LIS PENDENS"
    assert rec.party_name == "STUART PAUL ALAN"
    assert rec.date_recorded == "08/21/2026"
    assert rec.enrichment_data == {"instrument_number": "202608210171"}


def test_trustee_sale_row_is_labelled_trustee_sale_after_party_orientation():
    # Live 2026-09-01 shape: the trustee is [R], the borrower is [E]; orientation
    # moves the person into party_name and the doc type must still be captured.
    rec = _pre_fc_row(
        "TRUSTEE SALE", "[R] QUALITY LOAN SERVICE CORP [E] ALCAZAR DAVID P (+)",
        legal="COMMUNITY LT 41", inst="202609010223", filed="09/01/2026",
    )
    assert rec is not None
    assert rec.doc_type == "TRUSTEE SALE"
    assert rec.party_name == "ALCAZAR DAVID P"
    assert rec.heirs == "QUALITY LOAN SERVICE CORP"


def test_notice_of_default_and_foreclosure_rows_keep_their_labels():
    assert _pre_fc_row("NOTICE OF DEFAULT").doc_type == "NOTICE OF DEFAULT"
    rec = _pre_fc_row(
        "NOTICE OF FORECLOSURE", "[R] DELGADO LIZETTE (+) [E] HIDDEN GLEN MHC LLC",
        legal="5000050810", inst="202606250205", filed="06/25/2026",
    )
    assert rec.doc_type == "NOTICE OF FORECLOSURE"


def test_unrecognised_grid_type_falls_back_to_category_label():
    # A cell that only PARTIALLY matches (or a future ARMS relabel) must never be
    # stored as a guessed type — the category label stays.
    rec = _pre_fc_row("NOTICE OF TRUSTEES SALE")
    assert rec.doc_type == "PRE-FORECLOSURE"


def test_narrowed_doc_types_only_accept_the_searched_labels():
    # An explicit Notice-of-Trustee-Sale selection searches only checkbox 324, so
    # a grid cell reading LIS PENDENS is not a searched type and must not be adopted.
    scraper = PierceWAPreForeclosureScraper(doc_types=["notice_of_trustee_sale"])
    assert scraper._map_row(_grid_cells("LIS PENDENS", "[R] STUART PAUL ALAN [E] SEIM TERRY")).doc_type == "PRE-FORECLOSURE"
    assert scraper._map_row(_grid_cells("TRUSTEE SALE", "[R] QUALITY LOAN SERVICE CORP [E] ALCAZAR DAVID P")).doc_type == "TRUSTEE SALE"


def test_probate_rows_are_unchanged():
    # Single-type searches keep the configured label (no behaviour change).
    rec = PierceWAProbateScraper()._map_row(
        _grid_cells("PROBATE", "[R] DOE JOHN [E] DOE JANE", legal="ASHTON VILLAGE LT 7 (+)")
    )
    assert rec is not None
    assert rec.doc_type == "PROBATE"


def test_label_map_covers_every_pre_foreclosure_checkbox():
    ids = PierceWAARMSScraper.RECORD_TYPE_CONFIG["pre_foreclosure"]["ids"]
    assert set(ids) == set(PierceWAARMSScraper.ARMS_DOC_TYPE_LABELS)
