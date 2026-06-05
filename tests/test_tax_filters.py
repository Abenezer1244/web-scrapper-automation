"""Phase 4 — tax view/export filter math (months delinquent <-> bill_year).

Pure unit tests (no DB). The DB application (predicates ANDed into get_results /
download) is exercised in CI; here we lock the months<->bill_year arithmetic
that is easy to get off-by-one and the predicate-building shape.
"""
from datetime import date
from decimal import Decimal

from src.api.tax_filters import bill_year_bounds_for_months, build_tax_conditions

# A fixed "today" so the math is deterministic (no Date.now in tests).
TODAY = date(2026, 6, 15)  # base = 2026*12 + 5 = 24317


class TestBillYearBounds:
    def test_no_filters(self):
        assert bill_year_bounds_for_months(None, None, TODAY) == (None, None)

    def test_min_months_maps_to_max_year(self):
        # >= 0 months delinquent -> bill_year <= floor(base/12) = this year.
        max_year, min_year = bill_year_bounds_for_months(0, None, TODAY)
        assert max_year == 2026
        assert min_year is None

    def test_min_months_18_excludes_recent_years(self):
        # 2026-06: a 2025 bill (issued 2025-01-01) is ~17 months delinquent,
        # a 2024 bill ~29 months. ">= 18 months" must keep <=2024, drop 2025.
        max_year, _ = bill_year_bounds_for_months(18, None, TODAY)
        assert max_year == 2024

    def test_min_months_12_keeps_last_year(self):
        # 2025 bill ~17 months -> >= 12 keeps 2025 (<=2025).
        max_year, _ = bill_year_bounds_for_months(12, None, TODAY)
        assert max_year == 2025

    def test_max_months_maps_to_min_year(self):
        # <= 17 months delinquent -> bill_year >= 2025 (2025 is ~17mo, 2024 ~29).
        _, min_year = bill_year_bounds_for_months(None, 17, TODAY)
        assert min_year == 2025

    def test_range_both_bounds(self):
        max_year, min_year = bill_year_bounds_for_months(12, 30, TODAY)
        # 12..30 months -> bill_year in [2024, 2025]
        assert (max_year, min_year) == (2025, 2024)

    def test_year_boundary_january(self):
        # January: base = year*12 + 0; >=0 months -> <= that year.
        jan = date(2026, 1, 10)
        max_year, _ = bill_year_bounds_for_months(0, None, jan)
        assert max_year == 2026
        # 12 months in Jan 2026: 2025 bill is exactly 12 months -> kept.
        max_year12, _ = bill_year_bounds_for_months(12, None, jan)
        assert max_year12 == 2025


class TestBuildConditions:
    def test_empty_when_no_filters(self):
        assert build_tax_conditions(None, None, None, None, TODAY) == []

    def test_amount_only(self):
        conds = build_tax_conditions(Decimal("1000"), None, None, None, TODAY)
        assert len(conds) == 1

    def test_amount_range(self):
        conds = build_tax_conditions(100, 5000, None, None, TODAY)
        assert len(conds) == 2

    def test_amount_and_months_range(self):
        conds = build_tax_conditions(100, None, 12, 30, TODAY)
        # 1 amount + 2 year bounds
        assert len(conds) == 3

    def test_min_months_only_one_year_bound(self):
        conds = build_tax_conditions(None, None, 18, None, TODAY)
        assert len(conds) == 1
