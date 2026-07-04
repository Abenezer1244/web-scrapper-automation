"""Trustee Sale lean export columns — auction block kept, irrelevant types dropped."""
from src.utils.lead_export import LEAD_CSV_COLUMNS, resolve_lead_export_columns


class TestLeanColumns:
    def test_keeps_auction_block(self):
        cols = resolve_lead_export_columns("trustee_sale")
        for c in ("auction_date", "days_to_auction", "default_amount", "trustee", "ts_number"):
            assert c in cols

    def test_drops_other_type_specific_columns(self):
        cols = resolve_lead_export_columns("trustee_sale")
        # tax / code_violation / probate-only columns can never populate for an
        # auction lead, so the lean export omits them.
        for c in (
            "delinquent_amount", "tax_account_status", "code_violation_type",
            "lead_subtype",
        ):
            assert c not in cols

    def test_is_ordered_subset_of_canonical(self):
        cols = resolve_lead_export_columns("trustee_sale")
        assert cols == [c for c in LEAD_CSV_COLUMNS if c in set(cols)]
        # trustee_sale is a known type -> lean subset, strictly smaller than full.
        assert len(cols) < len(LEAD_CSV_COLUMNS)
