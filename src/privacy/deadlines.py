"""Response deadlines for privacy requests (Phase 6 P8, design section 3).

DEFAULTS ADOPTED BY THE OWNER, PENDING COUNSEL (D1-D9 are policy, not legal advice):
  opt_out                    15 business days
  delete, access, correct    45 calendar days
counted from the day after receipt, in California time (America/Los_Angeles), ending at
the end of the last day. Business days skip weekends and the US federal holidays, with a
Saturday holiday observed on the Friday before and a Sunday one on the Monday after.

Every deadline is stored with RULES_VERSION (privacy_requests.deadline_rules): changing a
rule means a new version, and requests already received keep the deadline they were given.
Extensions are recorded by the operator as an 'extended' event (migration 125).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

RULES_VERSION = "ccpa-2026"
_TZ = ZoneInfo("America/Los_Angeles")
_BUSINESS_DAYS = {"opt_out": 15}
_CALENDAR_DAYS = {"delete": 45, "access": 45, "correct": 45}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    return d - timedelta(days=1) if d.weekday() == 5 else (
        d + timedelta(days=1) if d.weekday() == 6 else d)


def federal_holidays(year: int) -> frozenset[date]:
    """The observed US federal holidays in ``year`` (5 U.S.C. 6103)."""
    mon, thu = 0, 3
    days = {
        _observed(date(year, 1, 1)),                    # New Year's Day
        _nth_weekday(year, 1, mon, 3),                  # Birthday of Martin Luther King, Jr.
        _nth_weekday(year, 2, mon, 3),                  # Washington's Birthday
        _last_weekday(year, 5, mon),                    # Memorial Day
        _observed(date(year, 6, 19)),                   # Juneteenth
        _observed(date(year, 7, 4)),                    # Independence Day
        _nth_weekday(year, 9, mon, 1),                  # Labor Day
        _nth_weekday(year, 10, mon, 2),                 # Columbus Day
        _observed(date(year, 11, 11)),                  # Veterans Day
        _nth_weekday(year, 11, thu, 4),                 # Thanksgiving Day
        _observed(date(year, 12, 25)),                  # Christmas Day
    }
    # Next year's New Year's Day on a Saturday is observed on 31 December of this year.
    days.add(_observed(date(year + 1, 1, 1)))
    return frozenset(d for d in days if d.year == year)


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in federal_holidays(d.year)


def deadline_for(kind: str, received_at: datetime) -> datetime:
    """The end of the last day to respond, as an aware datetime (California time)."""
    if received_at.tzinfo is None:
        raise ValueError("received_at must be timezone-aware")
    day = received_at.astimezone(_TZ).date()
    if kind in _BUSINESS_DAYS:
        left = _BUSINESS_DAYS[kind]
        while left:
            day += timedelta(days=1)
            if is_business_day(day):
                left -= 1
    elif kind in _CALENDAR_DAYS:
        day += timedelta(days=_CALENDAR_DAYS[kind])
    else:
        raise ValueError(f"unknown request kind {kind!r}")
    return datetime.combine(day, time(23, 59, 59), _TZ)
