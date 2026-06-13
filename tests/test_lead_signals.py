"""Pure-function tests for derived lead signals (src/utils/lead_signals.py).

Every case injects an explicit `today` so assertions never depend on the clock
(testing rule: no hardcoded now()). Mirrors the months math in tax_filters.
"""
from datetime import date

from src.utils.lead_signals import (
    contactability_score,
    derive_signals,
    freshness_days,
    months_delinquent,
    wa_foreclosure_eligible,
)

TODAY = date(2026, 6, 12)  # base = 2026*12 + 5 = 24317


class TestMonthsDelinquent:
    def test_none_bill_year(self):
        assert months_delinquent(None, TODAY) is None

    def test_three_full_years(self):
        # bill_year 2023, today 2026-06 -> 24317 - 2023*12(=24276) = 41 months
        assert months_delinquent(2023, TODAY) == 41

    def test_current_year_small(self):
        assert months_delinquent(2026, TODAY) == 5

    def test_future_year_unclamped(self):
        # exact tax_filters parity (Codex): no floor — future bill_year is negative
        assert months_delinquent(2030, TODAY) < 0

    def test_matches_tax_filters_base_formula(self):
        base = TODAY.year * 12 + (TODAY.month - 1)
        assert months_delinquent(2020, TODAY) == base - 2020 * 12


class TestWaForeclosureEligible:
    def test_none_is_false(self):
        assert wa_foreclosure_eligible(None, TODAY) is False

    def test_exactly_three_years_eligible(self):
        assert wa_foreclosure_eligible(2023, TODAY) is True  # 2026-3 = 2023

    def test_two_years_not_eligible(self):
        assert wa_foreclosure_eligible(2024, TODAY) is False

    def test_old_year_eligible(self):
        assert wa_foreclosure_eligible(2018, TODAY) is True


class TestFreshnessDays:
    def test_from_parsed_date(self):
        assert freshness_days(date(2026, 6, 1), None, TODAY) == 11

    def test_from_mdy_string_fallback(self):
        assert freshness_days(None, "05/13/2026", TODAY) == 30

    def test_unparseable_is_none(self):
        assert freshness_days(None, "not a date", TODAY) is None
        assert freshness_days(None, None, TODAY) is None

    def test_future_filing_clamped_zero(self):
        assert freshness_days(date(2026, 12, 1), None, TODAY) == 0

    def test_parsed_date_preferred_over_string(self):
        # parsed wins even when the string says something else
        assert freshness_days(date(2026, 6, 10), "01/01/2000", TODAY) == 2


class TestContactabilityScore:
    def test_empty(self):
        assert contactability_score(None, None, None, None) == 0

    def test_primary_only(self):
        assert contactability_score("2065551234", "a@x.com", None, None) == 2

    def test_dedup_primary_against_array(self):
        # primary phone repeated in the array counts once
        score = contactability_score(
            "(206) 555-1234",
            "a@x.com",
            [{"number": "206-555-1234"}, {"number": "2535559999"}],
            ["a@x.com", "b@x.com"],
        )
        assert score == 2 + 2  # 2 distinct phones + 2 distinct emails

    def test_dedup_e164_vs_local(self):
        # +1-prefixed 11-digit and local 10-digit of the SAME number count once
        score = contactability_score(
            "+1 (206) 555-1234", None, [{"number": "2065551234"}], None
        )
        assert score == 1

    def test_months_formula_unclamped_matches_filter(self):
        # exact parity with tax_filters: future bill_year goes negative, not 0
        assert months_delinquent(2030, TODAY) == TODAY.year * 12 + (TODAY.month - 1) - 2030 * 12

    def test_object_phones_tolerated(self):
        class _PC:
            def __init__(self, number):
                self.number = number
        score = contactability_score(None, None, [_PC("2065550000"), _PC("2065551111")], None)
        assert score == 2

    def test_capped_at_three_each(self):
        score = contactability_score(
            "1", "x@a.com",
            [{"number": str(n)} for n in (10, 11, 12, 13, 14)],
            [f"{c}@a.com" for c in "bcdef"],
        )
        assert score == 6  # 3 + 3 cap


class TestDeriveSignals:
    def test_dict_record(self):
        rec = {
            "delinquent_bill_year": 2022,
            "date_recorded": "06/01/2026",
            "phone": "2065551234",
            "email": None,
            "phones": [{"number": "2065551234"}, {"number": "2535550000"}],
            "emails": [],
        }
        sig = derive_signals(rec, TODAY)
        assert sig["months_delinquent"] == months_delinquent(2022, TODAY)
        assert sig["wa_foreclosure_eligible"] is True
        assert sig["freshness_days"] == 11
        assert sig["contactability_score"] == 2  # 2 distinct phones, 0 emails

    def test_non_tax_record_blank_tax_signals(self):
        sig = derive_signals({"date_recorded": "06/12/2026"}, TODAY)
        assert sig["months_delinquent"] is None
        assert sig["wa_foreclosure_eligible"] is False
        assert sig["freshness_days"] == 0
        assert sig["contactability_score"] == 0


class TestDaysToAuction:
    def test_future_auction(self):
        from datetime import date

        from src.utils.lead_signals import days_to_auction
        assert days_to_auction(date(2026, 7, 10), date(2026, 6, 12)) == 28

    def test_none_when_no_auction(self):
        from src.utils.lead_signals import days_to_auction
        assert days_to_auction(None, TODAY) is None

    def test_past_auction_clamped(self):
        from datetime import date

        from src.utils.lead_signals import days_to_auction
        assert days_to_auction(date(2026, 1, 1), date(2026, 6, 12)) == 0
