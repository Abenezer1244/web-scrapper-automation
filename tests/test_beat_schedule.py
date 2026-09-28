"""Beat schedule rules that a redeploy-heavy production depends on.

Beat's schedule file is not on a volume, so every deploy starts every entry fresh,
and a fresh interval entry waits one full period before its first run. On
2026-09-15 (UTC) beat containers started at 00:59:20, 01:11:56, 01:19:35 and
01:29:25, and the 20-minute recover-deferred-property sweep never fired. These
tests replay those boots with a controlled clock against the real schedule objects.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from celery.schedules import crontab, schedule

from src.workers.scheduler import app

_SLOW_S = 600
_KING_SWEEPS = ("recover-deferred-mailing", "recover-deferred-owners", "recover-deferred-property",
                "recover-code-violation-owners")
# Beat container start times read from the Railway beat deployment logs.
_BOOTS = [datetime(2026, 9, 15, 0, 59, 20, tzinfo=UTC), datetime(2026, 9, 15, 1, 11, 56, tzinfo=UTC),
          datetime(2026, 9, 15, 1, 19, 35, tzinfo=UTC), datetime(2026, 9, 15, 1, 29, 25, tzinfo=UTC)]
_LIFETIMES = list(zip(_BOOTS[:-1], _BOOTS[1:], strict=True))


def _seconds(sched) -> float | None:
    """Interval length for an interval entry, None for a crontab."""
    if isinstance(sched, crontab):
        return None
    if isinstance(sched, (int, float)):
        return float(sched)
    if isinstance(sched, timedelta):
        return sched.total_seconds()
    if isinstance(sched, schedule):
        return sched.run_every.total_seconds()
    raise AssertionError(f"unexpected schedule type {type(sched)!r}")


def _with_clock(sched, clock):
    """The same schedule, reading time from `clock` instead of the wall clock."""
    def now():
        return clock[0]
    if isinstance(sched, crontab):
        return crontab(minute=sched._orig_minute, hour=sched._orig_hour,
                       day_of_week=sched._orig_day_of_week, day_of_month=sched._orig_day_of_month,
                       month_of_year=sched._orig_month_of_year, nowfun=now, app=app)
    return schedule(timedelta(seconds=_seconds(sched)), nowfun=now, app=app)


def _fires_between(sched, start: datetime, stop: datetime) -> bool:
    """Would a beat booted fresh at `start` send this entry before `stop`?"""
    clock = [start]
    probe = _with_clock(sched, clock)
    while clock[0] < stop:
        if probe.is_due(start).is_due:   # a fresh entry's last_run_at is its boot time
            return True
        clock[0] += timedelta(seconds=30)
    return False


def _sub_daily_crontabs() -> list[str]:
    return [name for name, e in app.conf.beat_schedule.items()
            if isinstance(e["schedule"], crontab) and len(e["schedule"].hour) == 24]


def _longest_gap(sched: crontab) -> timedelta:
    marks = sorted(sched.minute)
    return timedelta(minutes=max((marks[(i + 1) % len(marks)] - m) % 60 or 60 for i, m in enumerate(marks)))


def test_no_slow_entry_is_a_plain_interval():
    slow = {name: s for name, e in app.conf.beat_schedule.items()
            if (s := _seconds(e["schedule"])) is not None and s >= _SLOW_S}
    assert slow == {}, f"interval entries of {_SLOW_S}s or more restart on every deploy: {slow}"


@pytest.mark.parametrize("name", _sub_daily_crontabs())
def test_a_fresh_boot_runs_every_sub_daily_crontab_within_its_own_step(name):
    """However often beat restarts, a crontab never waits more than its own step."""
    sched = app.conf.beat_schedule[name]["schedule"]
    step = _longest_gap(sched)
    for boot in _BOOTS:
        assert _fires_between(sched, boot, boot + step + timedelta(seconds=30)), (name, boot)


def test_the_property_sweep_fires_through_the_real_deploy_burst():
    sched = app.conf.beat_schedule["recover-deferred-property"]["schedule"]
    assert any(_fires_between(sched, *life) for life in _LIFETIMES)


def test_the_replay_is_honest_the_old_interval_starved():
    """The pre-fix 1200 s interval fires in none of those container lifetimes."""
    assert not any(_fires_between(1200.0, *life) for life in _LIFETIMES)


def test_king_recovery_sweeps_never_share_or_crowd_a_minute():
    marks = {name: set(app.conf.beat_schedule[name]["schedule"].minute) for name in _KING_SWEEPS}
    for i, a in enumerate(_KING_SWEEPS):
        for b in _KING_SWEEPS[i + 1:]:
            gap = min(min((x - y) % 60, (y - x) % 60) for x in marks[a] for y in marks[b])
            assert gap >= 2, f"{a} and {b} start {gap} min apart on the shared King lease"


def test_king_recovery_sweeps_keep_their_cadence():
    per_hour = {name: len(app.conf.beat_schedule[name]["schedule"].minute) for name in _KING_SWEEPS}
    assert per_hour == {"recover-deferred-mailing": 6, "recover-deferred-owners": 4,
                        "recover-deferred-property": 3, "recover-code-violation-owners": 3}


def test_the_skip_trace_dispatcher_runs_on_its_interval_setting(monkeypatch):
    """The beat entry and the pause-state heartbeat read ONE setting (Phase 1b-1b-iii),
    so they can never disagree. Rebuilt with a non-default value, so a hardcoded 300
    fails."""
    import importlib

    from src.config import settings
    from src.workers import scheduler

    monkeypatch.setattr(settings, "SKIP_TRACE_DISPATCH_INTERVAL_SECONDS", 120)
    try:
        importlib.reload(scheduler)
        entry = app.conf.beat_schedule["dispatch-pending-skip-trace"]
        assert _seconds(entry["schedule"]) == 120.0
    finally:
        monkeypatch.undo()
        importlib.reload(scheduler)
    assert _seconds(app.conf.beat_schedule["dispatch-pending-skip-trace"]["schedule"]) == float(
        settings.SKIP_TRACE_DISPATCH_INTERVAL_SECONDS)


def test_the_dispatch_interval_defaults_to_the_old_300_seconds():
    from src.config.settings import Settings

    assert Settings.model_fields["SKIP_TRACE_DISPATCH_INTERVAL_SECONDS"].default == 300


@pytest.mark.parametrize("value,ok", [(59, False), (60, True), (599, True), (600, False)])
def test_the_dispatch_interval_is_refused_at_boot_outside_60_to_599(value, ok):
    """600+ would be a plain interval that restarts on every deploy (see above)."""
    from pydantic import ValidationError

    from src.config.settings import Settings

    if ok:
        assert Settings(SKIP_TRACE_DISPATCH_INTERVAL_SECONDS=value).SKIP_TRACE_DISPATCH_INTERVAL_SECONDS == value
    else:
        with pytest.raises(ValidationError, match="SKIP_TRACE_DISPATCH_INTERVAL_SECONDS"):
            Settings(SKIP_TRACE_DISPATCH_INTERVAL_SECONDS=value)


def test_beat_runs_in_utc():
    assert app.conf.timezone == "UTC"
    assert app.conf.enable_utc is True
