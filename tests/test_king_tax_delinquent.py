"""Tests for the King County tax-delinquent aggregation (pure, no network).

`aggregate_delinquent_rows` is the heart of the rewritten King scraper: it turns
the Socrata "Delinquent Taxes" rows (one unpaid receivable LINE per charge type
per year) into one record per parcel, summing (billed - paid) across all
included charge types and delinquent years — matching Snohomish's methodology.

Fixtures use Socrata-shaped row dicts with zero-padded cent strings, mirroring
the live dsv3-ct3e payload. The HTTP pagination path is exercised by the live
Railway smoke run; here we lock the money math + gating that the old
`receivable_type='D'` bug got wrong.
"""
from decimal import Decimal

from src.scrapers.king_wa_tax_delinquent import aggregate_delinquent_rows


def _row(acct, year, rtype, billed, paid):
    """Build a Socrata-shaped row dict (cent strings, zero-padded like live)."""
    return {
        "account_number": acct,
        "bill_year": str(year),
        "receivable_type": rtype,
        "billed_amount": f"{billed:010d}",
        "paid_amount": f"{paid:010d}",
    }


# Parcel A (0123456789): delinquent across 2023+2024, multiple charge types,
# spread over TWO 12-digit accounts that share the same 10-digit parcel; plus an
# abatement line (excluded) and an unknown-code line (excluded).
# Parcel B: single year, single charge. Parcel C: net-zero (fully paid) -> dropped.
FIXTURE_ROWS = [
    _row("012345678900", 2024, "R", 1418785, 0),        # $14,187.85 main tax
    _row("012345678900", 2024, "U", 122553, 0),         # $1,225.53 surface water
    _row("012345678900", 2024, "D", 9152, 0),           # $91.52 drainage
    _row("012345678900", 2023, "R", 1000000, 200000),   # $8,000.00 (partial pay)
    _row("012345678900", 2024, "A", 50000000, 0),       # abatement -> EXCLUDED
    _row("012345678900", 2024, "Z", 5000, 0),           # unknown code -> EXCLUDED
    _row("012345678955", 2024, "N", 831, 0),            # $8.31, SAME parcel A
    _row("022222222200", 2024, "R", 300000, 0),         # Parcel B $3,000.00
    _row("033333333300", 2024, "R", 100000, 100000),    # Parcel C net $0 -> drop
    # malformed accounts (quarantined, never collapse into a parcel)
    _row("0044444", 2024, "R", 5000, 0),                # 7-digit personal property
    _row("ABCDEFGHIJKL", 2024, "R", 5000, 0),           # 12 chars but non-numeric
    {"account_number": "012345678977", "bill_year": "notayear",
     "receivable_type": "R", "billed_amount": "0000005000", "paid_amount": "0000000000"},
    # out-of-range years (excluded by the year window, not counted malformed)
    _row("055555555500", 2099, "R", 5000, 0),           # future
    _row("066666666600", 2019, "R", 5000, 0),           # before start
]


def _by_parcel(records):
    return {r.parcel_id: r for r in records}


def test_aggregates_all_charge_types_and_years_per_parcel():
    records, stats = aggregate_delinquent_rows(
        FIXTURE_ROWS, start_year=2020, effective_end_year=2024
    )
    by = _by_parcel(records)

    # Only the two parcels with net-positive owed; net-zero parcel C dropped.
    assert set(by) == {"0123456789", "0222222222"}

    a = by["0123456789"].enrichment_data
    # 14187.85 + 1225.53 + 91.52 (2024, acct ...00) + 8000.00 (2023) + 8.31 (acct ...55)
    assert a["delinquent_amount"] == "23513.21"
    assert Decimal(a["delinquent_amount"]) == Decimal("23513.21")  # no float drift
    assert a["bill_year"] == 2023            # oldest delinquent year
    assert a["oldest_tax_year"] == 2023
    assert a["delinquent_years"] == [2023, 2024]
    assert a["delinquent_year_count"] == 2
    assert a["source"] == "king_county_delinquent_taxes"
    assert by["0123456789"].date_recorded == "01/01/2023"
    # both 12-digit accounts that share the parcel are recorded
    assert a["account_numbers"] == ["012345678900", "012345678955"]


def test_charge_type_and_year_breakdown_kept():
    records, _ = aggregate_delinquent_rows(
        FIXTURE_ROWS, start_year=2020, effective_end_year=2024
    )
    a = _by_parcel(records)["0123456789"].enrichment_data
    # R = 14187.85 (2024) + 8000.00 (2023) = 22187.85; abatement/unknown excluded.
    assert a["amount_by_charge_type"] == {
        "D": "91.52", "N": "8.31", "R": "22187.85", "U": "1225.53",
    }
    assert "A" not in a["amount_by_charge_type"]
    assert "Z" not in a["amount_by_charge_type"]
    assert a["amount_by_year"] == {"2023": "8000.00", "2024": "15513.21"}


def test_abatement_and_unknown_excluded_and_alerted():
    _, stats = aggregate_delinquent_rows(
        FIXTURE_ROWS, start_year=2020, effective_end_year=2024
    )
    assert stats["abatement_rows"] == 1
    assert stats["abatement_nonzero"] == 1          # the $500k A row -> alert
    assert stats["unknown_type_rows"] == 1
    assert stats["unknown_codes"] == {"Z"}


def test_malformed_accounts_quarantined():
    _, stats = aggregate_delinquent_rows(
        FIXTURE_ROWS, start_year=2020, effective_end_year=2024
    )
    # 7-digit, non-numeric, and bad-year rows are quarantined, never parcels.
    assert stats["skipped_malformed_acct"] == 3
    assert stats["total_rows"] == len(FIXTURE_ROWS)


def test_single_charge_parcel_amount_and_label():
    records, _ = aggregate_delinquent_rows(
        FIXTURE_ROWS, start_year=2020, effective_end_year=2024
    )
    b = _by_parcel(records)["0222222222"]
    assert b.enrichment_data["delinquent_amount"] == "3000.00"
    assert b.enrichment_data["bill_year"] == 2024
    assert b.parcel_id == "0222222222"
    assert b.party_name == "Tax Delinquent — $3,000 owed (Parcel 0222222222)"
    # doc_type stays None (like Snoho tax) so cached-records `doc_type IS NULL`
    # keeps these rows visible.
    assert b.doc_type is None


def test_current_year_and_out_of_range_excluded():
    # effective_end_year=2024 must drop the 2099 future row; start_year=2020 drops
    # the 2019 row. Neither should create a parcel.
    records, _ = aggregate_delinquent_rows(
        FIXTURE_ROWS, start_year=2020, effective_end_year=2024
    )
    by = _by_parcel(records)
    assert "0555555555" not in by   # 2099 future bill
    assert "0666666666" not in by   # 2019 before window


def test_empty_input_is_clean():
    records, stats = aggregate_delinquent_rows(
        [], start_year=2020, effective_end_year=2024
    )
    assert records == []
    assert stats["total_rows"] == 0
    assert stats["unknown_codes"] == set()


# ─── 18-month product cap (drop parcels whose OLDEST unpaid year is too old) ───

# Parcel OLD (oldest year 2010) vs Parcel NEW (oldest year 2025). The cap drops
# a parcel by its OLDEST delinquent year, so OLD must go even though it also
# carries a 2025 line (recency-over-volume trade, user decision 2026-06-16).
_CAP_ROWS = [
    _row("011111111100", 2010, "R", 100000, 0),   # Parcel OLD: oldest = 2010
    _row("011111111100", 2025, "R", 200000, 0),   # ...also delinquent 2025
    _row("022222222200", 2025, "R", 300000, 0),   # Parcel NEW: oldest = 2025
]


def test_cap_drops_parcel_with_old_oldest_year():
    records, stats = aggregate_delinquent_rows(
        _CAP_ROWS, start_year=2000, effective_end_year=2025, cap_min_year=2025
    )
    by = _by_parcel(records)
    assert set(by) == {"0222222222"}          # NEW kept
    assert "0111111111" not in by             # OLD dropped (oldest 2010 < 2025)
    assert stats["capped_out"] == 1
    assert by["0222222222"].enrichment_data["bill_year"] == 2025


def test_cap_keeps_parcel_at_boundary_year():
    # A parcel whose oldest year EQUALS cap_min_year is kept (>= cutoff).
    rows = [_row("033333333300", 2025, "R", 100000, 0)]
    records, stats = aggregate_delinquent_rows(
        rows, start_year=2000, effective_end_year=2025, cap_min_year=2025
    )
    assert _by_parcel(records)["0333333333"].enrichment_data["bill_year"] == 2025
    assert stats["capped_out"] == 0


def test_cap_none_disables_cap_back_compat():
    # cap_min_year=None (default) must leave every parcel — back-compat.
    records, stats = aggregate_delinquent_rows(
        _CAP_ROWS, start_year=2000, effective_end_year=2025
    )
    by = _by_parcel(records)
    assert set(by) == {"0111111111", "0222222222"}
    assert stats["capped_out"] == 0
    assert by["0111111111"].enrichment_data["bill_year"] == 2010
