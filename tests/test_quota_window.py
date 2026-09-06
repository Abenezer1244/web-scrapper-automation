"""Entitlement-window arithmetic: real calendar months, UTC, and one definition.

The period rule used to be written out seven times across the API, the worker's
reservation and settlement statements, the release path and the beat. Two of
those comparисons were already wrong for any anchor that is not the 1st. So the
arithmetic now lives in exactly two places — ``src/api/quota_window.py`` for
Python and the ``public.quota_*`` functions from migration 088 for SQL — and the
last test in this file is the one that matters most: it proves the two agree
over a generated matrix of anchors and instants, so they cannot drift apart in
production without CI noticing.

Real Postgres, real settings, no mocks — per the project testing rules.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from src.api.quota_window import (
    add_months,
    effective_window,
    grid_index,
    is_frozen,
    next_window,
    should_roll,
    transitional_end,
    window_containing,
)
from src.db.session import SyncSessionLocal


def _dt(y, m, d, hh=0, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


class _FakeUser:
    """Plain attribute bag — the module reads a user duck-typed, never the ORM,
    so the pure functions stay unit-testable without a database round trip."""

    def __init__(self, **kw):
        self.subscription_status = None
        self.entitlement_grace_ends_at = None
        self.entitlement_ends_at = None
        self.quota_anchor_at = None
        self.quota_period_start = None
        self.quota_period_end = None
        for k, v in kw.items():
            setattr(self, k, v)


# ─── Month arithmetic ─────────────────────────────────────────────────────────

def test_jan_31_anchor_walks_real_calendar_months_without_compounding():
    """Jan 31 -> Feb 28 -> Mar 31 -> Apr 30 -> May 31.

    The failure this pins is the tempting one: adding a month to the PREVIOUS
    boundary instead of to the anchor. That gives Feb 28 -> Mar 28 and silently
    moves the customer's reset day to the 28th forever.
    """
    anchor = _dt(2026, 1, 31)
    assert [add_months(anchor, k) for k in range(5)] == [
        _dt(2026, 1, 31),
        _dt(2026, 2, 28),
        _dt(2026, 3, 31),
        _dt(2026, 4, 30),
        _dt(2026, 5, 31),
    ]


def test_leap_year_february_gets_the_29th():
    assert add_months(_dt(2028, 1, 31), 1) == _dt(2028, 2, 29)
    assert add_months(_dt(2026, 1, 31), 1) == _dt(2026, 2, 28)


def test_month_add_preserves_time_of_day():
    """A conversion at 14:37 must not silently move the reset to midnight."""
    assert add_months(_dt(2026, 3, 20, 14, 37), 1) == _dt(2026, 4, 20, 14, 37)


def test_add_months_crosses_the_year_boundary():
    assert add_months(_dt(2026, 11, 15), 3) == _dt(2027, 2, 15)
    assert add_months(_dt(2026, 12, 31), 2) == _dt(2027, 2, 28)


# ─── Grid index / containment ─────────────────────────────────────────────────

def test_grid_index_is_exact_across_a_clamped_cell():
    """The naive month difference is wrong here, which is why grid_index
    corrects rather than trusting it: Feb 28 12:00 is inside cell 1, not 0."""
    anchor = _dt(2026, 1, 31)
    assert grid_index(anchor, _dt(2026, 1, 31)) == 0
    assert grid_index(anchor, _dt(2026, 2, 27)) == 0
    assert grid_index(anchor, _dt(2026, 2, 28, 12)) == 1
    assert grid_index(anchor, _dt(2026, 3, 1)) == 1
    assert grid_index(anchor, _dt(2026, 3, 31)) == 2


def test_window_containing_is_half_open():
    anchor = _dt(2026, 9, 20, 8, 5)
    start, end = window_containing(anchor, _dt(2026, 10, 20, 8, 5))
    # The boundary instant belongs to the NEW window, never the old one.
    assert start == _dt(2026, 10, 20, 8, 5)
    assert end == _dt(2026, 11, 20, 8, 5)


# ─── Rollover ─────────────────────────────────────────────────────────────────

def test_steady_state_rollover_is_exactly_one_month():
    anchor = _dt(2026, 9, 20)
    start, end = next_window(anchor, _dt(2026, 10, 20), _dt(2026, 10, 20, 0, 1))
    assert (start, end) == (_dt(2026, 10, 20), _dt(2026, 11, 20))


def test_rollover_is_contiguous_with_the_window_it_replaces():
    """No gap and no overlap: a new window starts where the old one ended, so no
    consumption can fall between two windows and go unmetered."""
    anchor = _dt(2026, 1, 31)
    end_1 = add_months(anchor, 1)
    start, end = next_window(anchor, end_1, end_1 + timedelta(seconds=1))
    assert start == end_1
    assert end == add_months(anchor, 2)


def test_three_months_away_grants_exactly_one_window_not_three():
    """Unused entitlement must not accumulate. Someone who disappears in
    September and returns in December gets December's bucket, not four."""
    anchor = _dt(2026, 9, 10)
    start, end = next_window(anchor, _dt(2026, 10, 10), _dt(2026, 12, 25))
    assert (start, end) == (_dt(2026, 12, 10), _dt(2027, 1, 10))


def test_anchor_move_produces_one_bounded_transitional_window():
    """The cutover case. A subscriber leaves a legacy calendar window ending
    Oct 1 with a newly backfilled anchor on the 2nd.

    Continuing naively to the next boundary would give [Oct 1, Oct 2) — a
    one-day window with a full bucket, immediately followed by another. Snapping
    to the CLOSEST boundary gives one sane transitional window instead.
    """
    anchor = _dt(2026, 9, 2)
    start, end = next_window(anchor, _dt(2026, 10, 1), _dt(2026, 10, 1))
    assert start == _dt(2026, 10, 1)
    assert end == _dt(2026, 11, 2)
    assert timedelta(days=25) < (end - start) < timedelta(days=46)
    # ...and the very next rollover is already back on the grid.
    start2, end2 = next_window(anchor, end, end)
    assert (start2, end2) == (_dt(2026, 11, 2), _dt(2026, 12, 2))


def test_transitional_window_is_never_empty_or_backwards():
    """Swept over every anchor day against every legacy month end: the window
    that begins at old_end must always be a real forward interval."""
    for anchor_day in range(1, 29):
        for month in range(1, 13):
            anchor = _dt(2026, month, anchor_day)
            for end_month in range(1, 13):
                old_end = _dt(2026, end_month, 1)
                end = transitional_end(anchor, old_end)
                assert end > old_end, (anchor, old_end, end)


def test_transitional_window_stays_within_a_fortnight_of_a_month():
    """The bound that makes the one-off cutover shift defensible in either
    direction — no user gains or loses more than about half a month, once."""
    for anchor_day in (1, 2, 5, 14, 15, 16, 20, 28):
        anchor = _dt(2026, 3, anchor_day)
        for end_day in (1, 9, 17, 25, 28):
            old_end = _dt(2026, 6, end_day)
            length = transitional_end(anchor, old_end) - old_end
            assert timedelta(days=14) <= length <= timedelta(days=47), (
                anchor, old_end, length
            )


# ─── Freeze / roll predicates ─────────────────────────────────────────────────

def test_starter_and_free_accounts_are_never_frozen():
    """A NULL subscription status has no subscription to fail. Freezing free
    users would be a pure self-inflicted outage."""
    assert is_frozen(_FakeUser(subscription_status=None)) is False
    assert is_frozen(_FakeUser(subscription_status="")) is False
    assert is_frozen(_FakeUser(subscription_status="active")) is False


@pytest.mark.parametrize(
    "status", ["unpaid", "incomplete", "incomplete_expired", "paused"]
)
def test_non_entitled_statuses_freeze_immediately(status):
    assert is_frozen(_FakeUser(subscription_status=status)) is True


def test_past_due_is_served_through_its_grace_then_frozen():
    now = _dt(2026, 9, 10)
    inside = _FakeUser(
        subscription_status="past_due", entitlement_grace_ends_at=_dt(2026, 9, 12)
    )
    beyond = _FakeUser(
        subscription_status="past_due", entitlement_grace_ends_at=_dt(2026, 9, 9)
    )
    assert is_frozen(inside, now) is False
    assert is_frozen(beyond, now) is True


def test_frozen_account_does_not_roll_so_it_cannot_accrue_free_buckets():
    user = _FakeUser(
        subscription_status="unpaid",
        quota_anchor_at=_dt(2026, 8, 1),
        quota_period_start=_dt(2026, 8, 1),
        quota_period_end=_dt(2026, 9, 1),
    )
    assert should_roll(user, _dt(2026, 9, 5)) is False
    # Recovery releases it, and lands on ONE window — not one per frozen month.
    user.subscription_status = "active"
    assert should_roll(user, _dt(2026, 9, 5)) is True
    assert effective_window(user, _dt(2026, 11, 5)) == (
        _dt(2026, 11, 1),
        _dt(2026, 12, 1),
    )


def test_scheduled_cancellation_stops_the_window_at_the_entitlement_end():
    user = _FakeUser(
        subscription_status="active",
        entitlement_ends_at=_dt(2026, 10, 20),
        quota_anchor_at=_dt(2026, 8, 20),
        quota_period_start=_dt(2026, 9, 20),
        quota_period_end=_dt(2026, 10, 20),
    )
    # At the boundary the paid term is over: no new window is opened.
    assert should_roll(user, _dt(2026, 10, 20, 0, 1)) is False
    # Once the downgrade lands and the field is cleared, it rolls normally.
    user.entitlement_ends_at = None
    assert should_roll(user, _dt(2026, 10, 20, 0, 1)) is True


def test_effective_window_reports_the_rolled_window_before_anything_writes():
    """/billing/usage must not show a stale window just because no job has run
    since the boundary."""
    user = _FakeUser(
        subscription_status="active",
        quota_anchor_at=_dt(2026, 3, 20),
        quota_period_start=_dt(2026, 8, 20),
        quota_period_end=_dt(2026, 9, 20),
    )
    assert effective_window(user, _dt(2026, 9, 21)) == (
        _dt(2026, 9, 20),
        _dt(2026, 10, 20),
    )
    assert effective_window(user, _dt(2026, 9, 19)) == (
        _dt(2026, 8, 20),
        _dt(2026, 9, 20),
    )


def test_naive_datetimes_are_read_as_utc_not_local():
    """Some drivers hand back naive values from timestamptz columns. Reading one
    as local time would shift every boundary by the host's offset."""
    user = _FakeUser(
        subscription_status="active",
        quota_anchor_at=datetime(2026, 3, 20),
        quota_period_start=datetime(2026, 8, 20),
        quota_period_end=datetime(2026, 9, 20),
    )
    assert effective_window(user, _dt(2026, 9, 21)) == (
        _dt(2026, 9, 20),
        _dt(2026, 10, 20),
    )


# ─── THE agreement test: Python must equal Postgres ───────────────────────────

_MATRIX_ANCHORS = [
    _dt(2026, 1, 31),          # the clamping anchor
    _dt(2028, 1, 31),          # ...through a leap February
    _dt(2026, 1, 1),           # legacy calendar grid
    _dt(2026, 2, 28),
    _dt(2028, 2, 29),          # leap-day anchor
    _dt(2026, 3, 30, 23, 59),  # DST-transition day in both US and EU zones
    _dt(2026, 9, 20, 14, 37),  # a realistic subscription anchor
    _dt(2026, 10, 31, 6, 0),
    _dt(2025, 12, 31),
]

_MATRIX_INSTANTS = [
    _dt(2026, 1, 1),
    _dt(2026, 2, 28, 12),
    _dt(2026, 3, 1),
    _dt(2026, 3, 29, 2, 30),   # inside the EU DST jump
    _dt(2026, 9, 20, 14, 37),
    _dt(2026, 9, 20, 14, 36),
    _dt(2026, 11, 1, 1, 30),   # inside the US DST fall-back
    _dt(2027, 2, 28),
    _dt(2028, 2, 29, 23, 59),
    _dt(2029, 6, 15),
]


def test_python_and_postgres_window_math_agree_over_a_date_matrix():
    """The single most important test in this file.

    Python drives ``/billing/usage`` and the enforcement gates; the SQL
    functions drive the atomic reserve, settle, release and reconcile
    statements. If they ever disagree, production hands out or withholds quota
    with a green test suite. So every pair is compared against real Postgres —
    including clamped anchors, a leap day, and instants inside both the EU and
    US daylight-saving transitions, since Postgres month arithmetic on a
    timestamptz is evaluated in the SESSION timezone unless it is re-cast.
    """
    checked = 0
    with SyncSessionLocal() as db:
        for anchor in _MATRIX_ANCHORS:
            for at in _MATRIX_INSTANTS:
                row = db.execute(
                    text(
                        "SELECT public.quota_grid_index(:a, :t) AS k, "
                        "       public.quota_add_months(:a, 3) AS plus3, "
                        "       public.quota_transitional_end(:a, :t) AS tend"
                    ),
                    {"a": anchor, "t": at},
                ).one()
                assert row.k == grid_index(anchor, at), (anchor, at, row.k)
                assert _utc(row.plus3) == add_months(anchor, 3), (anchor, at)
                assert _utc(row.tend) == transitional_end(anchor, at), (anchor, at)

                # next_window uses the previous window's END as its old_end, so
                # feed the matrix instant in that role too.
                nxt = db.execute(
                    text(
                        "SELECT public.quota_next_start(:a, :e, :t) AS s, "
                        "       public.quota_next_end(:a, :e, :t) AS e"
                    ),
                    {"a": anchor, "e": at, "t": at + timedelta(days=3)},
                ).one()
                py = next_window(anchor, at, at + timedelta(days=3))
                assert (_utc(nxt.s), _utc(nxt.e)) == py, (anchor, at, nxt, py)
                checked += 1
    assert checked == len(_MATRIX_ANCHORS) * len(_MATRIX_INSTANTS)


def test_postgres_should_roll_matches_the_python_predicate():
    cases = [
        # (period_end, status, grace, ends_at, now, expected)
        (_dt(2026, 9, 1), None, None, None, _dt(2026, 9, 2), True),
        (_dt(2026, 9, 1), "active", None, None, _dt(2026, 8, 30), False),
        (_dt(2026, 9, 1), "unpaid", None, None, _dt(2026, 9, 2), False),
        (_dt(2026, 9, 1), "paused", None, None, _dt(2026, 9, 2), False),
        (_dt(2026, 9, 1), "past_due", _dt(2026, 9, 5), None, _dt(2026, 9, 2), True),
        (_dt(2026, 9, 1), "past_due", _dt(2026, 9, 1), None, _dt(2026, 9, 2), False),
        (_dt(2026, 9, 1), "past_due", None, None, _dt(2026, 9, 2), True),
        (_dt(2026, 9, 1), "active", None, _dt(2026, 9, 1), _dt(2026, 9, 2), False),
        (_dt(2026, 9, 1), "active", None, _dt(2026, 10, 1), _dt(2026, 9, 2), True),
    ]
    with SyncSessionLocal() as db:
        for end, status, grace, ends_at, now, expected in cases:
            sql = db.execute(
                text(
                    "SELECT public.quota_should_roll(:e, :s, :g, :x, :n) AS ok"
                ),
                {"e": end, "s": status, "g": grace, "x": ends_at, "n": now},
            ).scalar()
            user = _FakeUser(
                subscription_status=status,
                entitlement_grace_ends_at=grace,
                entitlement_ends_at=ends_at,
                quota_period_end=end,
            )
            assert sql is expected, (end, status, grace, ends_at, now, sql)
            assert should_roll(user, now) is expected, (status, grace, ends_at)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
