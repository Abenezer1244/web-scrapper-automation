"""Unit tests for _should_run_now — the schedule day/time matcher.

Proves the weekly day-of-week and monthly day-of-month selectors (Q1 fix),
the short-month clamp, back-compat defaults (Monday / the 1st), and the
defensive coercion of bad persisted JSON values. Pure logic — no DB/broker.

Calendar anchors used below (all 2026):
  - 2026-06-12 is a Friday  (weekday 4)
  - 2026-06-15 is a Monday  (weekday 0)
  - 2026-06-17 is a Wednesday (weekday 2)
  - 2026-06-01 is a Monday  (monthly back-compat default)
  - February 2026 has 28 days (not a leap year); June has 30.
"""
from datetime import UTC, datetime

from src.workers.scheduler_helpers.dispatch import (
    _coerce_schedule_int,
    _should_run_now,
)


def _dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def test_daily_fires_at_target_with_drift_tolerance():
    assert _should_run_now("daily", _dt(2026, 6, 12, 6, 0), 6, 0) is True
    assert _should_run_now("daily", _dt(2026, 6, 12, 6, 1), 6, 0) is True   # +1 drift
    assert _should_run_now("daily", _dt(2026, 6, 12, 5, 59), 6, 0) is True  # -1 drift
    assert _should_run_now("daily", _dt(2026, 6, 12, 6, 2), 6, 0) is False  # outside window


def test_weekly_fires_only_on_chosen_weekday():
    # Wednesday selected (run_weekday=2)
    assert _should_run_now("weekly", _dt(2026, 6, 17, 6, 0), 6, 0, run_weekday=2) is True
    # Friday, but Wednesday was chosen -> no fire
    assert _should_run_now("weekly", _dt(2026, 6, 12, 6, 0), 6, 0, run_weekday=2) is False


def test_weekly_default_is_monday_backcompat():
    # A config saved before the picker existed has no run_at_weekday -> default 0 (Monday)
    assert _should_run_now("weekly", _dt(2026, 6, 15, 6, 0), 6, 0) is True   # Monday
    assert _should_run_now("weekly", _dt(2026, 6, 12, 6, 0), 6, 0) is False  # Friday


def test_monthly_fires_only_on_chosen_day():
    assert _should_run_now("monthly", _dt(2026, 6, 15, 6, 0), 6, 0, run_day_of_month=15) is True
    assert _should_run_now("monthly", _dt(2026, 6, 14, 6, 0), 6, 0, run_day_of_month=15) is False


def test_monthly_clamps_to_last_day_of_short_month():
    # Feb 2026 has 28 days; choosing the 31st fires on the 28th, not never.
    assert _should_run_now("monthly", _dt(2026, 2, 28, 6, 0), 6, 0, run_day_of_month=31) is True
    assert _should_run_now("monthly", _dt(2026, 2, 27, 6, 0), 6, 0, run_day_of_month=31) is False


def test_monthly_default_is_first_backcompat():
    assert _should_run_now("monthly", _dt(2026, 6, 1, 6, 0), 6, 0) is True
    assert _should_run_now("monthly", _dt(2026, 6, 2, 6, 0), 6, 0) is False


def test_manual_and_unknown_frequency_never_fire():
    assert _should_run_now("manual", _dt(2026, 6, 12, 6, 0), 6, 0) is False
    assert _should_run_now("dailyx", _dt(2026, 6, 12, 6, 0), 6, 0) is False


def test_defensive_clamp_of_bad_persisted_values():
    # Hand-edited / legacy JSON could hold junk — it must not crash the beat.
    # Non-coercible junk falls back to defaults (Monday / the 1st); out-of-range
    # numbers clamp into range. Neither fires on a nonsense day.
    assert _should_run_now("weekly", _dt(2026, 6, 15, 6, 0), 6, 0, run_weekday="x") is True   # "x" -> 0 (Mon)
    assert _should_run_now("monthly", _dt(2026, 6, 1, 6, 0), 6, 0, run_day_of_month=None) is True  # None -> 1
    # Out-of-range high day clamps to 31, then to the month's length (June=30).
    assert _should_run_now("monthly", _dt(2026, 6, 30, 6, 0), 6, 0, run_day_of_month=99) is True


def test_coerce_schedule_int_normalizes_and_falls_back():
    assert _coerce_schedule_int(6, 0, 23, 6) == 6        # valid int passes
    assert _coerce_schedule_int("7", 0, 23, 6) == 7      # numeric string coerced
    assert _coerce_schedule_int(99, 0, 23, 6) == 23      # clamped to high bound
    assert _coerce_schedule_int(-5, 0, 23, 6) == 0       # clamped to low bound
    assert _coerce_schedule_int(None, 0, 23, 6) == 6     # junk -> fallback
    assert _coerce_schedule_int("nope", 0, 23, 6) == 6   # junk -> fallback


def test_coerced_hour_minute_never_crash_occurrence_key():
    # The batch occurrence key does now.replace(hour=_coerce_schedule_int(...)).
    # Corrupted persisted values must normalize to a legal hour/minute so
    # now.replace can't raise (Codex P1).
    now = _dt(2026, 6, 12, 6, 0)
    for bad_hour, bad_min in [(None, None), ("99", "-1"), (99, 99), ("x", "y")]:
        hour = _coerce_schedule_int(bad_hour, 0, 23, 6)
        minute = _coerce_schedule_int(bad_min, 0, 59, 0)
        # Must not raise:
        now.replace(hour=hour, minute=minute, second=0, microsecond=0)
