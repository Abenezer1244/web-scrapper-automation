"""Phase 4 — tax view/export filter math (months delinquent <-> bill_year).

Pure unit tests (no DB). The DB application (predicates ANDed into get_results /
download) is exercised in CI; here we lock the months<->bill_year arithmetic
that is easy to get off-by-one and the predicate-building shape.
"""
from datetime import date, timedelta
from decimal import Decimal

from src.api.tax_filters import (
    bill_year_bounds_for_months,
    build_tax_conditions,
    tax_cap_min_year,
)
from src.utils.lead_signals import months_delinquent

# A fixed "today" so the math is deterministic (no Date.now in tests).
# Months are counted from May 1 of the bill year (RCW 84.56.020 first-half
# delinquency): anchor = 2026*12 + 5 - 4 = 24313, so a 2025 bill is 13 months
# delinquent and a 2024 bill 25.
TODAY = date(2026, 6, 15)


class TestBillYearBounds:
    def test_no_filters(self):
        assert bill_year_bounds_for_months(None, None, TODAY) == (None, None)

    def test_min_months_zero_adds_no_year_bound(self):
        # Every clamped months value is >= 0, so ">= 0 months" constrains no year.
        assert bill_year_bounds_for_months(0, None, TODAY) == (None, None)

    def test_min_months_18_excludes_recent_years(self):
        # 2025 is 13 months, 2024 is 25: ">= 18 months" keeps <= 2024.
        max_year, _ = bill_year_bounds_for_months(18, None, TODAY)
        assert max_year == 2024

    def test_min_months_12_keeps_last_year(self):
        max_year, _ = bill_year_bounds_for_months(12, None, TODAY)
        assert max_year == 2025

    def test_max_months_maps_to_min_year(self):
        # "<= 17 months" keeps 2025 (13) and drops 2024 (25).
        _, min_year = bill_year_bounds_for_months(None, 17, TODAY)
        assert min_year == 2025

    def test_range_both_bounds(self):
        max_year, min_year = bill_year_bounds_for_months(12, 30, TODAY)
        assert (max_year, min_year) == (2025, 2024)

    def test_january_counts_from_last_may(self):
        # January 2026: a 2025 bill went delinquent May 2025, so it is 8 months
        # old and ">= 12 months" no longer includes it (Jan 1 counting said 12).
        jan = date(2026, 1, 10)
        assert months_delinquent(2025, jan) == 8
        max_year12, _ = bill_year_bounds_for_months(12, None, jan)
        assert max_year12 == 2024

    def test_filter_and_displayed_months_agree_everywhere(self):
        # Parity: for every month of three years, every bill year, and every
        # min/max, the year bound selects exactly the rows whose DISPLAYED
        # (clamped) months value passes the same comparison.
        for year in (2026, 2027, 2028):
            for month in range(1, 13):
                today = date(year, month, 15)
                for bill_year in range(1990, 2031):
                    shown = months_delinquent(bill_year, today)
                    assert shown >= 0
                    for n in range(0, 61):
                        max_year, _ = bill_year_bounds_for_months(n, None, today)
                        in_min = max_year is None or bill_year <= max_year
                        assert in_min == (shown >= n), (today, bill_year, "min", n)
                        _, min_year = bill_year_bounds_for_months(None, n, today)
                        in_max = min_year is None or bill_year >= min_year
                        assert in_max == (shown <= n), (today, bill_year, "max", n)


class TestTaxCapMinYear:
    def test_last_year_is_never_capped_on_any_day_of_the_year(self):
        # Regression: from Aug 1 the calendar math alone returned the CURRENT year,
        # and a full tax roll can only call PRIOR years delinquent, so Snohomish
        # returned nothing from Aug 1 to Dec 31. Walk every day, leap year included.
        for year in (2026, 2028):
            day = date(year, 1, 1)
            while day.year == year:
                assert tax_cap_min_year(day) == year - 1, day
                day += timedelta(days=1)

    def test_two_years_back_stays_capped(self):
        # The floor only protects last year; the 18-month recency cap still drops
        # older delinquency (user decision 2026-06-16).
        assert tax_cap_min_year(date(2026, 1, 1)) > 2024
        assert tax_cap_min_year(date(2026, 12, 31)) > 2024

    def test_rolls_forward_at_the_new_year(self):
        assert tax_cap_min_year(date(2026, 12, 31)) == 2025
        assert tax_cap_min_year(date(2027, 1, 1)) == 2026


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
        # 1 amount + tax-rows-only + 2 year bounds
        assert len(conds) == 4

    def test_min_months_only_one_year_bound(self):
        conds = build_tax_conditions(None, None, 18, None, TODAY)
        # tax-rows-only + 1 year bound
        assert len(conds) == 2

    def test_min_months_zero_still_excludes_non_tax_rows(self):
        # No year bound for ">= 0", but the explicit NOT NULL keeps non-tax rows
        # out, as every other months filter does.
        conds = build_tax_conditions(None, None, 0, None, TODAY)
        assert len(conds) == 1
        assert "IS NOT NULL" in str(conds[0])
