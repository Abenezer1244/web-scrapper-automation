"""Lean per-record-type export column resolver (src/utils/lead_export.py).

Pure unit tests — no DB fixtures. Assert the STRUCTURAL column map: a single-type
export drops the columns that record type can never populate, keeps everything a
type can, preserves canonical order, and falls back to the full superset for
unknown/None record types (never silently drop data).
"""
import csv
import io

from src.utils.lead_export import (
    LEAD_CSV_COLUMNS,
    LEAN_BASE_COLUMNS,
    _TYPE_EXTRA_COLUMNS,
    resolve_lead_export_columns,
    write_lead_csv,
)

# Column blocks that belong to exactly one record type (see the map's rationale).
_TAX_BLOCK = {
    "delinquent_amount", "delinquent_bill_year", "tax_billed_amount",
    "tax_paid_amount", "tax_account_status", "months_delinquent",
    "wa_foreclosure_eligible",
}
_CODE_BLOCK = {
    "code_violation_type", "code_violation_status",
    "code_violation_description", "code_violation_last_inspection",
}
_NTS_BLOCK = {"auction_date", "days_to_auction", "default_amount", "trustee", "ts_number"}


def test_unknown_and_none_fall_back_to_full():
    assert resolve_lead_export_columns(None) == list(LEAD_CSV_COLUMNS)
    assert resolve_lead_export_columns("brand_new_type") == list(LEAD_CSV_COLUMNS)
    # A full export must be the complete canonical set, unchanged.
    assert resolve_lead_export_columns(None) == LEAD_CSV_COLUMNS


def test_every_type_is_ordered_subset_of_canonical():
    canonical_index = {c: i for i, c in enumerate(LEAD_CSV_COLUMNS)}
    for record_type in _TYPE_EXTRA_COLUMNS:
        cols = resolve_lead_export_columns(record_type)
        # subset
        assert set(cols).issubset(set(LEAD_CSV_COLUMNS))
        # no duplicates
        assert len(cols) == len(set(cols))
        # canonical order preserved
        positions = [canonical_index[c] for c in cols]
        assert positions == sorted(positions)


def test_base_columns_present_for_every_type():
    for record_type in _TYPE_EXTRA_COLUMNS:
        cols = set(resolve_lead_export_columns(record_type))
        assert set(LEAN_BASE_COLUMNS).issubset(cols), record_type


def test_probate_keeps_heirs_subtype_drops_other_type_blocks():
    cols = set(resolve_lead_export_columns("probate"))
    assert {"heirs", "lead_subtype"}.issubset(cols)
    assert not (_TAX_BLOCK & cols)
    assert not (_CODE_BLOCK & cols)
    assert not (_NTS_BLOCK & cols)


def test_tax_keeps_tax_block_drops_probate_and_others():
    cols = set(resolve_lead_export_columns("tax_delinquent"))
    assert _TAX_BLOCK.issubset(cols)
    assert not (_CODE_BLOCK & cols)
    assert not (_NTS_BLOCK & cols)
    # probate-only columns are dropped for tax
    assert "heirs" not in cols
    assert "lead_subtype" not in cols


def test_code_violation_keeps_only_its_block():
    cols = set(resolve_lead_export_columns("code_violation"))
    assert _CODE_BLOCK.issubset(cols)
    assert not (_TAX_BLOCK & cols)
    assert not (_NTS_BLOCK & cols)
    assert "heirs" not in cols


def test_pre_foreclosure_keeps_only_nts_block():
    cols = set(resolve_lead_export_columns("pre_foreclosure"))
    assert _NTS_BLOCK.issubset(cols)
    assert not (_TAX_BLOCK & cols)
    assert not (_CODE_BLOCK & cols)
    assert "heirs" not in cols


def test_divorce_and_death_cert_keep_heirs_not_subtype():
    for rt in ("divorce", "death_certificate"):
        cols = set(resolve_lead_export_columns(rt))
        assert "heirs" in cols, rt
        assert "lead_subtype" not in cols, rt


def test_eviction_is_base_only():
    assert resolve_lead_export_columns("eviction") == list(LEAN_BASE_COLUMNS)


def test_write_lead_csv_default_is_full_header():
    """Backward compat: no columns arg => the full 49-column header, unchanged."""
    out = io.StringIO()
    write_lead_csv([], out)
    header = out.getvalue().strip().split("\r\n")[0]
    assert header.split(",") == list(LEAD_CSV_COLUMNS)


def test_write_lead_csv_lean_header_and_values_match_full():
    """Lean file header = resolved subset; shared-column VALUES identical to full."""
    record = {
        "date_recorded": "2026-06-01",
        "party_name": "SMITH, JOHN",
        "parcel_id": "1234567890",
        "property_address": "123 MAIN ST, TACOMA, WA 98402",
        "heirs": "SMITH, JANE",
        # a tax-only field set on the record must NOT appear in a probate lean file
        "delinquent_amount": "999.99",
    }
    lean_cols = resolve_lead_export_columns("probate")

    lean_out = io.StringIO()
    write_lead_csv([record], lean_out, columns=lean_cols)
    lean_reader = csv.DictReader(io.StringIO(lean_out.getvalue()))
    lean_header = lean_reader.fieldnames
    lean_row = next(lean_reader)

    full_out = io.StringIO()
    write_lead_csv([record], full_out)
    full_reader = csv.DictReader(io.StringIO(full_out.getvalue()))
    full_row = next(full_reader)

    assert lean_header == lean_cols
    # probate lean file must not carry the tax column even though the record has it
    assert "delinquent_amount" not in lean_header
    assert "heirs" in lean_header

    # For columns present in BOTH files, the value must be identical (same builder).
    for col in lean_header:
        assert lean_row[col] == full_row[col], col
