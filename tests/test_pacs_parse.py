"""PACS owner-name results parser — over-inference guard (Codex point C).

`_parse_pacs_result_html` must (1) trust a result ONLY when the search returned
exactly one property, and (2) NEVER surface parcel_id (weak owner-name evidence
must not feed the parcel-primary property_key / billing dedup identity).

Pure parser over real HTML fixtures — no mocks, no network (per testing.md).
"""

from src.scrapers.enrichment.pacs import _parse_pacs_result_html

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
    out = _parse_pacs_result_html(html)
    assert out is not None
    assert out["address"] == "123 MAIN ST, OAK HARBOR WA 98277"


def test_single_result_never_returns_parcel_id():
    """Even though parcel + account cells are present, parcel_id must NOT surface."""
    html = _page(_row("9876543210", "1234567890", "123 MAIN ST, OAK HARBOR WA 98277", "DOE JANE"))
    out = _parse_pacs_result_html(html)
    assert out is not None
    assert "parcel_id" not in out


def test_single_result_with_a_stray_pager_row_still_parses():
    """A genuine single result plus a footer/pager row (few cells, no parcel#)
    must NOT be mis-counted as ambiguous (Codex P2 — plausible-row filter)."""
    pager = "<tr><td colspan=10>Page 1 of 1</td></tr>"
    html = _page(_row("9876543210", "1234567890", "5 BAY ST, OAK HARBOR WA 98277", "DOE JANE") + pager)
    out = _parse_pacs_result_html(html)
    assert out is not None
    assert out["address"] == "5 BAY ST, OAK HARBOR WA 98277"


def test_single_result_mailing_from_multiline_cell():
    """When the address cell carries newline-separated lines, mailing hydrates
    from the full multi-line value (the skip-trace payload)."""
    addr_cell = "742 EVERGREEN TER\nOAK HARBOR WA 98277"
    html = _page(_row("9876543210", "1234567890", addr_cell, "DOE JANE"))
    out = _parse_pacs_result_html(html)
    assert out is not None
    assert out["address"] == "742 EVERGREEN TER"
    assert out["mailing"] == "742 EVERGREEN TER, OAK HARBOR WA 98277"


def test_multiple_results_returns_none():
    """Ambiguous owner-name match (2 properties) → trust nothing."""
    html = _page(
        _row("1111111111", "2222222222", "1 FIR LN, COUPEVILLE WA 98239", "SMITH JOHN"),
        _row("3333333333", "4444444444", "2 OAK AVE, LANGLEY WA 98260", "SMITH JOHN A"),
    )
    assert _parse_pacs_result_html(html) is None


def test_no_results_table_returns_none():
    assert _parse_pacs_result_html("<html><body>None found</body></html>") is None


def test_header_only_no_data_row_returns_none():
    html = f"<html><table id='resultsTable'>{_HEADER}</table></html>"
    assert _parse_pacs_result_html(html) is None


def test_single_row_without_address_returns_none():
    """A unique row that has no parseable address yields nothing usable."""
    html = _page(_row("9876543210", "1234567890", "ADDRESS UNAVAILABLE", "DOE JANE"))
    assert _parse_pacs_result_html(html) is None


def test_uppercase_tags_still_counted():
    """Uppercase <TR>/<TD> tags must not be mis-read as zero data rows. (The
    control id 'resultsTable' is a fixed ASP.NET name and stays its real case.)"""
    row = (
        "<TR><TD><INPUT></TD><TD>9876543210</TD><TD>1234567890</TD><TD>REAL</TD>"
        "<TD>0010</TD><TD>9 PINE RD, FREELAND WA 98249</TD><TD>LOT 1</TD>"
        "<TD>DOE JANE</TD><TD>$1</TD><TD>VIEW</TD></TR>"
    )
    html = f"<html><table id='resultsTable'><TR><TH>HDR</TH></TR>{row}</table></html>"
    out = _parse_pacs_result_html(html)
    assert out is not None
    assert out["address"] == "9 PINE RD, FREELAND WA 98249"
    assert "parcel_id" not in out
