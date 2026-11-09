"""src/privacy/deadlines.py: business and calendar deadlines in California time."""
from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from src.privacy.deadlines import (
    RULES_VERSION,
    deadline_for,
    federal_holidays,
    is_business_day,
)

_LA = ZoneInfo("America/Los_Angeles")


def test_the_2026_federal_holidays_are_the_published_ones():
    # OPM's 2026 list: Jul 4 is a Saturday, observed Friday Jul 3.
    assert federal_holidays(2026) == {
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 5, 25),
        date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 10, 12),
        date(2026, 11, 11), date(2026, 11, 26), date(2026, 12, 25)}


def test_a_saturday_new_year_is_observed_on_the_last_day_of_the_year_before():
    assert date(2021, 12, 31) in federal_holidays(2021)     # 1 Jan 2022 was a Saturday
    assert date(2022, 1, 1) not in federal_holidays(2022)


def test_a_sunday_holiday_is_observed_on_monday():
    assert date(2027, 7, 5) in federal_holidays(2027)        # 4 Jul 2027 is a Sunday


def test_weekends_and_holidays_are_not_business_days():
    assert not is_business_day(date(2026, 11, 7))            # Saturday
    assert not is_business_day(date(2026, 11, 26))           # Thanksgiving
    assert is_business_day(date(2026, 11, 9))


def test_an_opt_out_gets_fifteen_business_days_skipping_thanksgiving():
    received = datetime(2026, 11, 20, 10, 0, tzinfo=_LA)     # a Friday
    # Nov 23-25 (3), 27 (4), 30 (5), Dec 1-4 (9), 7-11 (14), 14 (15).
    assert deadline_for("opt_out", received) == datetime(2026, 12, 14, 23, 59, 59, tzinfo=_LA)


@pytest.mark.parametrize("kind", ["delete", "access", "correct"])
def test_other_requests_get_forty_five_calendar_days(kind):
    received = datetime(2026, 11, 9, 9, 30, tzinfo=_LA)
    assert deadline_for(kind, received) == datetime(2026, 12, 24, 23, 59, 59, tzinfo=_LA)


def test_receipt_is_counted_in_california_not_utc():
    # 03:00 UTC on 10 Nov is still 9 Nov in California.
    received = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)
    assert deadline_for("delete", received).date() == date(2026, 12, 24)


def test_naive_times_and_unknown_kinds_are_refused():
    with pytest.raises(ValueError):
        deadline_for("delete", datetime(2026, 11, 9, 9, 30))
    with pytest.raises(ValueError):
        deadline_for("erase", datetime(2026, 11, 9, tzinfo=UTC))


def test_the_rules_version_fits_the_database_column():
    import re
    assert re.fullmatch(r"[a-z0-9_.-]{1,16}", RULES_VERSION)
