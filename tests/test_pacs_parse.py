"""PACS owner-name results parser — over-inference guard (Codex point C).

`parse_pacs_result_html` must (1) trust a result ONLY when the search returned
exactly one property, and (2) NEVER surface parcel_id (weak owner-name evidence
must not feed the parcel-primary property_key / billing dedup identity).

Pure parser over real HTML fixtures — no mocks, no network (per testing.md).
"""

from pathlib import Path

import pytest

from src.scrapers.enrichment.pacs import (
    compose_pacs_mailing,
    normalize_pacs_parcel,
    pacs_detail_url,
    parse_pacs_detail_html,
    parse_pacs_result_html,
)

# PACS PropertyAccess result columns: checkbox, account, parcel, type, tax_code,
# address, legal, owner, value, view. account AND parcel are both long numbers.
_HEADER = (
    "<tr><th>Sel</th><th>Account</th><th>Parcel</th><th>Type</th>"
    "<th>Tax Code</th><th>Address</th><th>Legal</th><th>Owner</th>"
    "<th>Value</th><th>View</th></tr>"
)


def _row(account: str, parcel: str, address: str, owner: str, value: str = "$250,000") -> str:
    return (
        f"<tr><td><input type=checkbox></td><td>{account}</td><td>{parcel}</td>"
        f"<td>Real</td><td>0010</td><td>{address}</td><td>LOT 1</td>"
        f"<td>{owner}</td><td>{value}</td><td>view</td></tr>"
    )


def _page(*rows: str) -> str:
    body = _HEADER + "".join(rows)
    return f"<html><body><table id='propertySearchResults_resultsTable'>{body}</table></body></html>"


def test_single_result_returns_address_and_mailing():
    html = _page(_row("9876543210", "1234567890", "123 MAIN ST, OAK HARBOR WA 98277", "DOE JANE"))
    out = parse_pacs_result_html(html)
    assert out is not None
    assert out["address"] == "123 MAIN ST, OAK HARBOR WA 98277"


def test_single_result_never_returns_parcel_id():
    """Even though parcel + account cells are present, parcel_id must NOT surface."""
    html = _page(_row("9876543210", "1234567890", "123 MAIN ST, OAK HARBOR WA 98277", "DOE JANE"))
    out = parse_pacs_result_html(html)
    assert out is not None
    assert "parcel_id" not in out


def test_single_result_with_a_stray_pager_row_still_parses():
    """A genuine single result plus a footer/pager row (few cells, no parcel#)
    must NOT be mis-counted as ambiguous (Codex P2 — plausible-row filter)."""
    pager = "<tr><td colspan=10>Page 1 of 1</td></tr>"
    html = _page(_row("9876543210", "1234567890", "5 BAY ST, OAK HARBOR WA 98277", "DOE JANE") + pager)
    out = parse_pacs_result_html(html)
    assert out is not None
    assert out["address"] == "5 BAY ST, OAK HARBOR WA 98277"


def test_grid_address_cell_is_the_situs_and_never_becomes_mailing():
    """The grid's multi-line address cell is the PROPERTY address with its locality.
    Until 2026-10-02 the second line turned it into ``mailing``, which copied the
    situs into results.mailing_address for every PACS hit (a fabricated
    owner-occupancy fact). The grid has no mailing column; only the detail page does."""
    addr_cell = "742 EVERGREEN TER\nOAK HARBOR WA 98277"
    html = _page(_row("9876543210", "1234567890", addr_cell, "DOE JANE"))
    out = parse_pacs_result_html(html)
    assert out is not None
    assert out["address"] == "742 EVERGREEN TER"
    assert "mailing" not in out


def test_single_result_carries_the_detail_links_prop_id():
    row = _row("9876543210", "1234567890", "742 EVERGREEN TER, OAK HARBOR WA 98277", "DOE JANE").replace(
        "<td>view</td>", '<td><a href="Property.aspx?cid=0&prop_id=58808&year=2026">View</a></td>')
    out = parse_pacs_result_html(_page(row))
    assert out is not None
    assert out["prop_id"] == "58808"


def test_single_result_without_a_detail_link_has_no_prop_id():
    out = parse_pacs_result_html(_page(_row("9876543210", "1234567890", "742 EVERGREEN TER, OAK HARBOR WA 98277", "DOE JANE")))
    assert out is not None
    assert "prop_id" not in out


# ─── Detail page: where the mailing address really is ────────────────────────

_FIXTURES = Path(__file__).parent / "fixtures"


def test_real_benton_detail_page_separates_mailing_from_situs():
    """Live Benton markup (2026-10-02, owner name redacted): the owner block's
    "Mailing Address:" and the property block's "Address:" are distinct cells."""
    page = parse_pacs_detail_html((_FIXTURES / "benton_pacs_detail_58808.html").read_text(encoding="utf-8"))
    assert page["geo_ids"] == ["131073011125003"]
    assert page["prop_ids"] == ["58808"]
    assert page["situs"] == ["65003 N SR 225", "BENTON CITY, WA 99320"]
    assert page["mailing"] == [["65003 N SR 225", "BENTON CITY, WA 99320"]]


def _detail(mailing_cells: list[str], *, geo_label: str = "Parcel # / Geo ID", geo: str = "131073011125003",
            situs: str = "65003 N SR 225 <BR> BENTON CITY, WA 99320") -> str:
    owners = "".join(
        f"<tr><td>Name:</td><td>OWNER</td><td>Owner ID:</td><td>1</td></tr>"
        f"<tr><td>Mailing Address:</td><td>{cell}</td><td>% Ownership:</td><td>100%</td></tr>"
        for cell in mailing_cells)
    return (f"<html><body><table><tr><td>Property ID:</td><td>58808</td></tr>"
            f"<tr><td>{geo_label}:</td><td>{geo}</td><td>Agent Code:</td><td></td></tr>"
            f"<tr><td>Address:</td><td>{situs}</td><td>Mapsco:</td><td></td></tr>"
            f"{owners}</table></body></html>")


def test_detail_parser_accepts_the_geographic_id_label_variant():
    page = parse_pacs_detail_html(_detail(["1 MAIN ST <BR> PORT ANGELES, WA 98362"], geo_label="Geographic ID",
                                          geo="0530084000100000"))
    assert page["geo_ids"] == ["0530084000100000"]
    assert page["mailing"] == [["1 MAIN ST", "PORT ANGELES, WA 98362"]]


def test_detail_parser_keeps_an_empty_mailing_cell_as_an_empty_block():
    page = parse_pacs_detail_html(_detail([""]))
    assert page["mailing"] == [[]]


def test_detail_parser_does_not_mistake_the_situs_label_for_mailing():
    page = parse_pacs_detail_html(_detail([]))
    assert page["mailing"] == []
    assert page["situs"] == ["65003 N SR 225", "BENTON CITY, WA 99320"]


@pytest.mark.parametrize("lines, expected", [
    (["65003 N SR 225", "BENTON CITY, WA 99320"], "65003 N SR 225, BENTON CITY, WA 99320"),
    (["PO BOX 800", "PHOENIX, AZ 85001"], "PO BOX 800, PHOENIX, AZ 85001"),
    (["P.O. BOX 12", "KENNEWICK WA 99336-0012"], "P.O. BOX 12, KENNEWICK, WA 99336-0012"),
    (["123 MAIN ST APT 4B", "SEATTLE, WA 98101"], "123 MAIN ST APT 4B, SEATTLE, WA 98101"),
    (["JOHN DOE REVOCABLE TRUST", "C/O JANE DOE", "9 PINE RD", "LANGLEY, WA 98260"], "9 PINE RD, LANGLEY, WA 98260"),
    (["12 RUE DE LA PAIX", "75002 PARIS", "FRANCE"], "12 RUE DE LA PAIX, 75002 PARIS, FRANCE"),
])
def test_compose_pacs_mailing(lines, expected):
    assert compose_pacs_mailing(lines) == expected


def test_compose_pacs_mailing_without_a_deliverable_line_is_none():
    assert compose_pacs_mailing(["SOME TRUST", "ANOTHER NAME"]) is None
    assert compose_pacs_mailing([]) is None


@pytest.mark.parametrize("raw, key", [
    ("131073011125003", "131073011125003"),
    ("0530084000100000", "530084000100000"),
    ("053008-400010-0000", "530084000100000"),
    ("05-3008 4000 100000", "530084000100000"),
    ("44042.0202", "440420202"),
    ("", None), (None, None), ("ABC123", None), ("0000", None),
])
def test_normalize_pacs_parcel(raw, key):
    assert normalize_pacs_parcel(raw) == key


def test_pacs_detail_url_keeps_origin_and_cid():
    assert pacs_detail_url("https://pacs.co.chelan.wa.us/PropertyAccess/?cid=91", "7") == \
        "https://pacs.co.chelan.wa.us/PropertyAccess/Property.aspx?cid=91&prop_id=7"
    assert pacs_detail_url("https://propertysearch.co.benton.wa.us/propertyaccess/PropertySearch.aspx?cid=0", "58808") == \
        "https://propertysearch.co.benton.wa.us/propertyaccess/Property.aspx?cid=0&prop_id=58808"


def test_multiple_results_returns_none():
    """Ambiguous owner-name match (2 properties) → trust nothing."""
    html = _page(
        _row("1111111111", "2222222222", "1 FIR LN, COUPEVILLE WA 98239", "SMITH JOHN"),
        _row("3333333333", "4444444444", "2 OAK AVE, LANGLEY WA 98260", "SMITH JOHN A"),
    )
    assert parse_pacs_result_html(html) is None


def test_no_results_table_returns_none():
    assert parse_pacs_result_html("<html><body>None found</body></html>") is None


def test_header_only_no_data_row_returns_none():
    html = f"<html><table id='resultsTable'>{_HEADER}</table></html>"
    assert parse_pacs_result_html(html) is None


def test_single_row_without_address_returns_none():
    """A unique row that has no parseable address yields nothing usable."""
    html = _page(_row("9876543210", "1234567890", "ADDRESS UNAVAILABLE", "DOE JANE"))
    assert parse_pacs_result_html(html) is None


def test_uppercase_tags_still_counted():
    """Uppercase <TR>/<TD> tags must not be mis-read as zero data rows. (The
    control id 'resultsTable' is a fixed ASP.NET name and stays its real case.)"""
    row = (
        "<TR><TD><INPUT></TD><TD>9876543210</TD><TD>1234567890</TD><TD>REAL</TD>"
        "<TD>0010</TD><TD>9 PINE RD, FREELAND WA 98249</TD><TD>LOT 1</TD>"
        "<TD>DOE JANE</TD><TD>$1</TD><TD>VIEW</TD></TR>"
    )
    html = f"<html><table id='resultsTable'><TR><TH>HDR</TH></TR>{row}</table></html>"
    out = parse_pacs_result_html(html)
    assert out is not None
    assert out["address"] == "9 PINE RD, FREELAND WA 98249"
    assert "parcel_id" not in out
