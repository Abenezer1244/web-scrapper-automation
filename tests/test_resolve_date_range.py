"""Tests for the tax-delinquent default date window in _resolve_date_range.

Asserts the WINDOW SIZE (date_to - date_from), which is invariant to the
starter-plan end-date delay, so no clock freezing is needed.
"""
from datetime import datetime

from src.workers.tasks_helpers.dates import (
    _TAX_DELINQUENT_DEFAULT_DAYS,
    _ordered_window,
    _resolve_date_range,
)


def _window_days(date_from: str, date_to: str) -> int:
    d0 = datetime.strptime(date_from, "%m/%d/%Y").date()
    d1 = datetime.strptime(date_to, "%m/%d/%Y").date()
    return (d1 - d0).days


def test_tax_delinquent_default_is_18_months():
    # Empty schedule => default path => ~18 months for tax_delinquent (any county).
    df, dt = _resolve_date_range({}, record_type="tax_delinquent")
    assert _window_days(df, dt) == _TAX_DELINQUENT_DEFAULT_DAYS


def test_tax_delinquent_rolling_90_still_18_months():
    # The schema default mode "rolling_90" maps to the default branch, which is
    # upgraded to 18 months for tax_delinquent.
    df, dt = _resolve_date_range({"date_range_mode": "rolling_90"}, record_type="tax_delinquent")
    assert _window_days(df, dt) == _TAX_DELINQUENT_DEFAULT_DAYS


def test_non_tax_record_keeps_90_day_default():
    df, dt = _resolve_date_range({}, record_type="probate")
    assert _window_days(df, dt) == 90
    # and with no record_type at all
    df2, dt2 = _resolve_date_range({})
    assert _window_days(df2, dt2) == 90


def test_explicit_narrow_mode_wins_over_tax_default():
    df, dt = _resolve_date_range({"date_range_mode": "rolling_30"}, record_type="tax_delinquent")
    assert _window_days(df, dt) == 30


def test_explicit_custom_dates_honored_for_tax():
    df, dt = _resolve_date_range(
        {"date_range_mode": "custom", "date_from": "2025-01-01", "date_to": "2025-06-30"},
        record_type="tax_delinquent",
    )
    assert df == "01/01/2025"
    assert dt == "06/30/2025"


def test_ordered_window_collapses_inverted_range():
    # A backwards window collapses to a single day at date_to (never inverted).
    assert _ordered_window("06/30/2025", "01/01/2025") == ("01/01/2025", "01/01/2025")
    # A correct window is untouched.
    assert _ordered_window("01/01/2025", "06/30/2025") == ("01/01/2025", "06/30/2025")
    # Equal dates (single day) are fine.
    assert _ordered_window("03/15/2025", "03/15/2025") == ("03/15/2025", "03/15/2025")


def test_custom_inverted_range_never_reaches_scraper():
    # The repro from the audit: 6/30 -> 1/1 must not pass through inverted.
    df, dt = _resolve_date_range(
        {"date_range_mode": "custom", "date_from": "2025-06-30", "date_to": "2025-01-01"},
    )
    assert df <= dt
    assert (df, dt) == ("01/01/2025", "01/01/2025")
