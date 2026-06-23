"""Phase 2: the probate lead_subtype honesty label reaches the delivered CSV.

build_lead_export_row reads the subtype the worker stamped into enrichment_data at
insert and emits it as a column, so a customer can see whether a "probate" row is a
real death or a living-owner Transfer-on-Death estate-planning record. Non-probate
rows (no lead_subtype in enrichment_data) export it blank.
"""

from src.utils.lead_export import (
    LEAD_CSV_COLUMNS,
    OVERLAP_LEAD_COLUMNS,
    build_lead_export_row,
    build_overlap_export_row,
)


def _record(enrichment_data: dict) -> dict:
    return {
        "party_name": "DOE JOHN",
        "doc_type": "Transfer on Death Deed",
        "property_address": "123 MAIN ST, SEATTLE, WA 98101",
        "date_recorded": "04/24/2026",
        "enrichment_data": enrichment_data,
    }


def test_lead_subtype_is_a_csv_column():
    assert "lead_subtype" in LEAD_CSV_COLUMNS


def test_living_owner_tod_row_is_labeled():
    out = build_lead_export_row(_record({"lead_subtype": "tod_living_owner_estate_planning"}))
    assert out["lead_subtype"] == "tod_living_owner_estate_planning"


def test_death_inheritance_row_is_labeled():
    out = build_lead_export_row(_record({"lead_subtype": "probate_death_inheritance"}))
    assert out["lead_subtype"] == "probate_death_inheritance"


def test_non_probate_row_subtype_is_blank():
    # No lead_subtype written (non-probate record types) -> blank column, header kept.
    out = build_lead_export_row(_record({}))
    assert out["lead_subtype"] == ""


def test_row_keys_exactly_match_columns():
    # csv.DictWriter requires the row dict keys to match the declared fieldnames.
    out = build_lead_export_row(_record({"lead_subtype": "probate_death_inheritance"}))
    assert set(out.keys()) == set(LEAD_CSV_COLUMNS)


def test_overlap_export_carries_lead_subtype():
    # Codex P2: combined/overlap (segment + batch) CSVs whitelist OVERLAP_LEAD_COLUMNS
    # and must NOT drop the honesty label.
    assert "lead_subtype" in OVERLAP_LEAD_COLUMNS
    out = build_overlap_export_row(
        _record({"lead_subtype": "tod_living_owner_estate_planning"}),
        {"lists_count": 2, "lists": "probate", "counties": "king"},
    )
    assert out["lead_subtype"] == "tod_living_owner_estate_planning"
    assert set(out.keys()) == set(OVERLAP_LEAD_COLUMNS)
