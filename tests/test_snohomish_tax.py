"""Tests for the Snohomish County tax-delinquent scraper parser + link resolver.

No network: the parser and link-selector are pure and tested against real rows
captured from the live Treasurer "Current Tax List" file and the real landing
page structure. (The HTTP download + redirect path is exercised by safe_http's
own tests and the live Railway smoke run.)
"""
from decimal import Decimal

import pytest

from src.scrapers.snohomish_wa_tax_delinquent import (
    _as_of_year,
    _select_current_tax_list_url,
    _to_decimal,
    parse_tax_list,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("06/01/2026", 2026),      # v17 format
        ("20260701", 2026),        # v15 format, live since 2026-07-01
        ("19960101", 1996),
        ("", None),
        ("   ", None),
        ("2026", None),            # bare year is not a date
        ("27060100400800", None),  # a 14-digit parcel must never read as a date
        ("20261301", None),        # month 13
        ("20260732", None),        # day 32
        ("2201.34", None),         # an amount must never read as a date
        # Real calendar validation, not a shape check — a corrupt cell must fail
        # closed rather than yield a year that reclassifies delinquency.
        ("99/99/2027", None),      # impossible month/day, valid-looking year
        ("13/01/2026", None),      # month 13 in the slash format
        ("02/30/2026", None),      # Feb 30 never exists
        ("20260229", None),        # 2026 is not a leap year
        ("01/01/1800", None),      # below the accepted year range
        ("01/01/2300", None),      # above the accepted year range
    ],
)
def test_as_of_year_accepts_both_published_formats(raw, expected):
    assert _as_of_year(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("507.83", Decimal("507.83")),
        ("271", Decimal("271")),
        ("$1,234.56", Decimal("1234.56")),
        ("0", Decimal("0")),
        ("", None),
        ("-5", None),                  # negative
        ("abc", None),
        ("99999999.99", Decimal("99999999.99")),   # at the ceiling
        ("100000000.00", None),        # over the Numeric(12,2) contract
        ("1e30", None),                # exponent form must not slip past the bound
        ("1.234", None),               # more precision than cents
    ],
)
def test_to_decimal_is_bounded(raw, expected):
    """Amounts come from an untrusted remote file and are summed per parcel.

    Unbounded values would reach enrichment_data (total_billed / full_year_levy)
    even though _extract_tax_fields bounds delinquent_amount downstream.
    """
    assert _to_decimal(raw) == expected

# Real rows from the live file (public records). Mix of: 7-digit personal
# property (excluded), 14-digit current-year (excluded), 14-digit $0-owed
# (excluded), 14-digit delinquent (kept), and one parcel delinquent across
# 2024+2025 with a 2026 current-year row that must NOT be aggregated.
FIXTURE_ROWS = """\
0002634|2022|19510 21ST AVE W||LYNNWOOD|WA|98036-4867|MILLWORX LLC|||LYNNWOOD|WA|98036|06/01/2026|507.83|507.83|507.83
00370800100101|2026|7914 180TH ST SW||EDMONDS|WA|98026-5419|LEE ANNA|||EDMONDS|WA|98026|06/01/2026|7398.84|3699.42|3699.42
00370300000001|2026|3703 ALASKA RD||BRIER|WA|98036|EVERGREEN WASHELLI MEMORIAL|||SEATTLE|WA|98133|06/01/2026|97.89|0|0
00370600000800|2025|6425 ADAMS LOG CABIN RD||SNOHOMISH|WA|98290-7300|CISSNA RICHARD C/KATHRYN A|||SNOHOMISH|WA|98290-7300|06/01/2026|117.03|60.01|60.01
00371400006102|2025|6426 SYCAMORE PL||EVERETT|WA|98203-4319|SCHWAB SUZANNE C|||EVERETT|WA|98203-4319|06/01/2026|4870.28|4870.28|4870.28
00371700101700|2025|8021 188TH ST SW||EDMONDS|WA|98026-6022|LORME CHARLES J & KATRINA A|||BELLEVUE|WA|98006|06/01/2026|5079.01|5079.01|5079.01
00371700101700|2024|8021 188TH ST SW||EDMONDS|WA|98026-6022|LORME CHARLES J & KATRINA A|||BELLEVUE|WA|98006|06/01/2026|5385.61|5385.61|5385.61
00371700101700|2026|8021 188TH ST SW||EDMONDS|WA|98026-6022|LORME CHARLES J & KATRINA A|||BELLEVUE|WA|98006|06/01/2026|5851.88|2947.72|5851.88
""".splitlines()


def _by_parcel(records):
    return {r.parcel_id: r for r in records}


def test_parse_filters_to_delinquent_real_property():
    # fallback_year=2099 proves the cutoff uses the file's as-of year (2026 in
    # col 13), NOT the fallback — else current-year 2026 rows would slip in.
    records, stats = parse_tax_list(FIXTURE_ROWS, fallback_year=2099)
    by = _by_parcel(records)

    # Exactly the 3 delinquent real-property parcels; personal-property,
    # current-year, and $0-owed rows excluded.
    assert set(by) == {"00370600000800", "00371400006102", "00371700101700"}
    assert "0002634" not in by          # 7-digit personal property
    assert "00370800100101" not in by   # 14-digit but current year (2026)
    assert "00370300000001" not in by   # 14-digit but $0 owed

    assert stats["total"] == 8
    assert stats["malformed"] == 0
    assert stats["delinquent_rows"] == 4  # CISSNA 1 + SCHWAB 1 + LORME 2
    assert stats["as_of_year"] == 2026


def test_parse_aggregates_multi_year_parcel():
    records, _ = parse_tax_list(FIXTURE_ROWS, fallback_year=2099)
    lorme = _by_parcel(records)["00371700101700"]
    ed = lorme.enrichment_data

    # 2024 (5385.61) + 2025 (5079.01) = 10464.62; the 2026 current-year row
    # (owed 5851.88) is excluded.
    assert ed["delinquent_amount"] == "10464.62"
    assert ed["bill_year"] == 2024            # oldest = most months delinquent
    assert ed["oldest_tax_year"] == 2024
    assert ed["delinquent_years"] == [2024, 2025]
    assert ed["delinquent_year_count"] == 2
    assert ed["source"] == "snohomish_county_delinquent_taxes"
    assert lorme.date_recorded == "01/01/2024"
    # delinquent_amount must round-trip cleanly to a Decimal (no float drift)
    assert Decimal(ed["delinquent_amount"]) == Decimal("10464.62")


def test_parse_single_year_amount_and_fields():
    records, _ = parse_tax_list(FIXTURE_ROWS, fallback_year=2099)
    cissna = _by_parcel(records)["00370600000800"]
    assert cissna.enrichment_data["delinquent_amount"] == "60.01"
    assert cissna.enrichment_data["bill_year"] == 2025
    # Human-readable fields map to first-class ScrapedRecord columns (the export
    # pipeline's sanitize_for_csv covers these — never raw-from-enrichment).
    assert cissna.party_name == "CISSNA RICHARD C/KATHRYN A"
    assert "SNOHOMISH" in cissna.property_address
    # doc_type stays None (like King tax) so the cached-records filter's
    # `doc_type IS NULL` branch keeps these rows visible (Codex P2).
    assert cissna.doc_type is None


def test_parse_counts_malformed_rows():
    rows = FIXTURE_ROWS + [
        "this|is|not|the|right|shape",            # wrong field count
        "00370600000801|notayear|x||C|WA|9|O|||C|WA|9|06/01/2026|1|1|1",  # bad year
    ]
    _, stats = parse_tax_list(rows, fallback_year=2099)
    assert stats["malformed"] == 2
    assert stats["total"] == 10


def test_parse_blank_lines_ignored():
    rows = ["", "   ", *FIXTURE_ROWS, ""]
    records, stats = parse_tax_list(rows, fallback_year=2099)
    assert stats["total"] == 8  # blanks not counted
    assert len(records) == 3


def test_v17_layout_is_detected():
    _, stats = parse_tax_list(FIXTURE_ROWS, fallback_year=2099)
    assert stats["layout"] == "v17_pre_2026_07"


# ─── v15 layout, live since 2026-07-01 (17 fields → 15) ───────────────────────
#
# Real rows from the live file. The county dropped both address "line 2" columns
# and the mailing STREET line, and added an amount column, so every column after
# the situs street shifted. Amounts are (billed, paid, owed, full-year levy) —
# owed is col 13, NOT the last column.
#
# Real rows from the live file (public records), including the leading all-empty
# record the file actually starts with.
FIXTURE_ROWS_V15 = """\
||||||||||||||
27060100400800|2026|315 S BLAKELEY ST|MONROE|WA|98272-2204|STEWART HEIDI L|MONROE|WA|98272-2204|20260701|2201.34|0|2201.34|4402.67
27060100417000|2026|518 S LEWIS ST|MONROE|WA|98272-2325|HOLZERLAND K|MONROE|WA|98272-2325|20260701|3850.52|1967.63|1882.89|3850.52
27060100417000|2025|518 S LEWIS ST|MONROE|WA|98272-2325|HOLZERLAND K|MONROE|WA|98272-2325|20260701|2207.33|1148.31|1059.02|2207.33
27060100401900|2026|207 S MADISON ST|MONROE|WA|98272-2216|PRICE DEANNA A|MONROE|WA|98272-2216|20260701|0|0|0|481.20
0006064|2023|21220 87TH AVE SE|WOODINVILLE|WA|98072-8002|T E O TECHNOLOGIES INC|MUKILTEO|WA| 98275|20260701|27.81|27.81|0|27.81
""".splitlines()


def test_v15_layout_detected_and_parsed():
    records, stats = parse_tax_list(FIXTURE_ROWS_V15, fallback_year=2099)
    assert stats["layout"] == "v15_2026_07"
    # as-of read from col 10 in 'YYYYMMDD' form, not the wall clock. fallback_year
    # 2099 proves the cutoff comes from the file, not the parameter.
    assert stats["as_of_year"] == 2026
    by = _by_parcel(records)
    # Only the prior-year (2025) real-property row with a balance is delinquent.
    assert set(by) == {"27060100417000"}
    assert "27060100400800" not in by   # 14-digit but current year (2026)
    assert "27060100401900" not in by   # 14-digit but $0 owed
    assert "0006064" not in by          # 7-digit personal property
    assert stats["total"] == 6
    assert stats["malformed"] == 1      # the leading all-empty record
    assert stats["delinquent_rows"] == 1


def test_v15_owed_is_not_the_last_column():
    """Regression: col 14 is the full-year levy, col 13 is what is still owed.

    Reading "the last amount" (as the v17 map did) would report 2207.33 instead
    of 1059.02 and overstate every delinquent balance.
    """
    records, _ = parse_tax_list(FIXTURE_ROWS_V15, fallback_year=2099)
    ed = _by_parcel(records)["27060100417000"].enrichment_data
    assert ed["delinquent_amount"] == "1059.02"
    assert ed["total_billed"] == "2207.33"      # billed-to-date, unchanged meaning
    assert ed["full_year_levy"] == "2207.33"
    assert ed["bill_year"] == 2025
    assert ed["source_layout"] == "v15_2026_07"


def test_v15_has_no_mailing_address_only_a_locality():
    """v15 publishes no mailing street, so mailing_address must stay None.

    compute_owner_flags() derives owner_state / absentee_owner /
    out_of_state_owner from the mailing address, so a city-only value would
    manufacture confident wrong signals. The locality is kept for audit only.
    """
    records, _ = parse_tax_list(FIXTURE_ROWS_V15, fallback_year=2099)
    rec = _by_parcel(records)["27060100417000"]
    assert rec.mailing_address is None
    assert rec.enrichment_data["mailing_locality"] == "MONROE WA 98272-2325"
    # the situs street IS published and must still be populated
    assert rec.property_address == "518 S LEWIS ST, MONROE WA 98272-2325"


def test_v17_blank_mailing_street_also_yields_no_mailing_address():
    """Same rule applied to v17: those rows carry the street columns BLANK."""
    records, _ = parse_tax_list(FIXTURE_ROWS, fallback_year=2099)
    rec = _by_parcel(records)["00370600000800"]
    assert rec.mailing_address is None
    assert rec.enrichment_data["mailing_locality"] == "SNOHOMISH WA 98290-7300"


def test_populated_mailing_street_would_be_kept():
    """CONSTRUCTED boundary case — no live row currently exercises this.

    Verified against the live v17 file: 0 of 328,069 rows populate the mailing
    street columns, and v15 drops them entirely, so Snohomish has never published
    a mailing street. This row is hand-built (like _CAP_ROWS below) purely to pin
    down the intended behaviour if the county ever restores the column: a real
    street-bearing address must flow through to mailing_address rather than being
    suppressed by the city-only rule.
    """
    row = (
        "00400000000004|2025|4 SITUS ST||EVERETT|WA|98201|MAIL OWNER|"
        "PO BOX 7|STE 2|SEATTLE|WA|98101|06/01/2026|100|100|100"
    ).splitlines()
    records, _ = parse_tax_list(row, fallback_year=2099)
    rec = _by_parcel(records)["00400000000004"]
    assert rec.mailing_address == "PO BOX 7 STE 2, SEATTLE WA 98101"
    assert "mailing_locality" not in rec.enrichment_data


def test_mixed_field_widths_count_as_malformed():
    """A half-swapped download must not be parsed as if it were consistent."""
    rows = [*FIXTURE_ROWS, *FIXTURE_ROWS_V15[1:]]
    _, stats = parse_tax_list(rows, fallback_year=2099)
    assert stats["layout"] == "v17_pre_2026_07"   # locked by the first rows
    assert stats["malformed"] == 5                # every 15-field row rejected


def test_unknown_field_width_is_all_malformed():
    rows = ["a|b|c", "d|e|f"]
    records, stats = parse_tax_list(rows, fallback_year=2099)
    assert records == []
    assert stats["malformed"] == 2
    assert stats["layout"] is None


# ─── landing-page link resolution (real page structure) ───────────────────────

_LANDING_HTML = (
    '<p>Current Tax List:&nbsp; To view the &ldquo;Current Tax List&rdquo;, which '
    'contains information for all parcels in Snohomish County and their current '
    'taxes due please click <a aria-describedby="x" '
    'href="/DocumentCenter/View/149173/snohomish_tax_data_totals" rel="noopener" '
    'target="_blank">here</a> (last updated 06/01/2026). For a description of the '
    'fields on the Current Tax list, please <a '
    'href="/DocumentCenter/View/148137/snohomish_tax_data_totals" '
    'target="_blank">click here</a>.</p>'
)


def test_select_picks_data_link_not_description_twin():
    url = _select_current_tax_list_url(_LANDING_HTML, "https://www.snohomishcountywa.gov/5568/Treasurer-Public-Records")
    assert url == (
        "https://www.snohomishcountywa.gov/DocumentCenter/View/149173/snohomish_tax_data_totals"
    )
    assert "148137" not in url  # the field-description twin is excluded


def test_select_survives_monthly_id_rotation():
    rotated = _LANDING_HTML.replace("149173", "200001").replace("148137", "200002")
    url = _select_current_tax_list_url(rotated, "https://www.snohomishcountywa.gov/5568/Treasurer-Public-Records")
    assert url.endswith("/DocumentCenter/View/200001/snohomish_tax_data_totals")


def test_select_raises_when_no_link():
    with pytest.raises(ValueError, match="no 'Current Tax List' download link"):
        _select_current_tax_list_url("<p>page changed, no link here</p>", "https://www.snohomishcountywa.gov/5568/Treasurer-Public-Records")


# ─── 18-month product cap (drop parcels whose OLDEST unpaid year is too old) ───

# as-of year 2026 (col 13) so both 2010 and 2025 are prior (delinquent) years.
# Parcel OLD is delinquent across 2010 + 2025; the cap drops it by its OLDEST
# year (2010) even though it carries a recent 2025 line (recency-over-volume,
# user decision 2026-06-16). Parcel NEW's oldest year is 2025.
_CAP_ROWS = """\
00100000000001|2010|1 OLD ST||EVERETT|WA|98201|OLD OWNER|||EVERETT|WA|98201|06/01/2026|100|100|100
00100000000001|2025|1 OLD ST||EVERETT|WA|98201|OLD OWNER|||EVERETT|WA|98201|06/01/2026|200|200|200
00200000000002|2025|2 NEW ST||EVERETT|WA|98201|NEW OWNER|||EVERETT|WA|98201|06/01/2026|300|300|300
""".splitlines()


def test_cap_drops_parcel_with_old_oldest_year():
    records, stats = parse_tax_list(_CAP_ROWS, fallback_year=2099, cap_min_year=2025)
    by = _by_parcel(records)
    assert set(by) == {"00200000000002"}          # NEW kept
    assert "00100000000001" not in by             # OLD dropped (oldest 2010 < 2025)
    assert stats["capped_out"] == 1
    assert by["00200000000002"].enrichment_data["bill_year"] == 2025


def test_cap_keeps_parcel_at_boundary_year():
    # A parcel whose oldest year EQUALS cap_min_year is kept (>= cutoff).
    rows = (
        "00300000000003|2025|3 EDGE ST||EVERETT|WA|98201|EDGE OWNER|||"
        "EVERETT|WA|98201|06/01/2026|100|100|100"
    ).splitlines()
    records, stats = parse_tax_list(rows, fallback_year=2099, cap_min_year=2025)
    assert _by_parcel(records)["00300000000003"].enrichment_data["bill_year"] == 2025
    assert stats["capped_out"] == 0


def test_cap_none_disables_cap_back_compat():
    # cap_min_year=None (default) must leave every parcel — back-compat.
    records, stats = parse_tax_list(_CAP_ROWS, fallback_year=2099)
    by = _by_parcel(records)
    assert set(by) == {"00100000000001", "00200000000002"}
    assert stats["capped_out"] == 0
    assert by["00100000000001"].enrichment_data["bill_year"] == 2010
