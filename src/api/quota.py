"""Effective record-quota usage — the one place the entitlement window is applied.

``users.records_used`` is only meaningful for the window named by
``users.quota_period_start`` / ``quota_period_end``. Reading the raw column is
therefore wrong whenever that window has ended: the number belongs to a PREVIOUS
entitlement month and the user is actually at 0 for the current one.

An ended window is a normal, expected state, not a corruption. Rollover is LAZY
— it happens inside the atomic statement that next charges the user, and an
hourly reconciliation catches up anyone who never transacts — so between the
true boundary and whichever comes first, a perfectly healthy user sits on a
window that has expired. The enforcement gates used to read the raw counter and
reject those users on usage they no longer owed.

This module is the single Python expression of the rule, so a gate cannot drift
from what the worker actually bills. The arithmetic itself lives in
``src/api/quota_window.py`` (and the matching ``public.quota_*`` SQL functions);
this file is only the read-side policy built on top of it.

WHAT CHANGED FROM THE CALENDAR RULE

Quota no longer resets on the 1st. It resets on the user's own entitlement
anniversary — the monthly grid anchored at ``users.quota_anchor_at`` — so a
subscriber who starts on the 20th is metered from the 20th, an annual subscriber
still gets a fresh month every month rather than 1,000 records for a year, and a
trial that converts to paid starts its paid allowance at conversion instead of
handing the customer nothing until the 1st.

There is now a second reason to refuse work that has nothing to do with usage:
``is_frozen``. A subscription that has stopped paying may not consume quota AND
its window does not advance, so it cannot quietly accrue a fresh bucket every
month. Callers should test that FIRST and say "payment required", because
telling a delinquent customer they are "over their limit" sends them to the
wrong remedy.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.api.quota_window import as_utc, effective_window, is_frozen, should_roll

__all__ = [
    "current_period_start",
    "effective_records_used",
    "effective_window",
    "is_frozen",
    "is_over_record_limit",
    "quota_block_reason",
]


def current_period_start(now: datetime | None = None) -> datetime:
    """First instant of the current UTC calendar month.

    DEPRECATED for quota. Kept only for the skip-trace counters, which are still
    metered on the calendar month against their own ``skip_trace_period_start``
    column and are deliberately out of scope for the entitlement-window change.
    Do not reach for this to answer a RECORD-quota question — use
    ``effective_window`` so the answer follows the user's own anniversary.
    """
    now = now or datetime.now(UTC)
    return now.astimezone(UTC).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )


def effective_records_used(user, now: datetime | None = None) -> int:
    """``user.records_used``, or 0 when its entitlement window has ended.

    Mirrors the ``base`` column of the worker's charging statements
    (``window_cte_sql`` in ``src/api/quota_window.py``). Keep the two in step —
    that is the entire reason both are expressed once.

    A user whose window has ended but who is FROZEN for non-payment keeps their
    counter: their window is not advancing, so the usage is still theirs. They
    are refused by ``is_frozen`` rather than by the counter, which is the honest
    reason.
    """
    used = user.records_used or 0
    if getattr(user, "quota_period_end", None) is None:
        # Unreachable after migration 088 (NOT NULL with a server_default).
        # Treat it as current rather than as a free reset: handing out quota is
        # the destructive direction, and the reconciliation alerts on any row it
        # cannot place in a window.
        return used
    return 0 if should_roll(user, now) else used


def is_over_record_limit(user, now: datetime | None = None) -> bool:
    """True when the user has consumed their plan's records for THIS window.

    ``records_limit == -1`` means unlimited and is never over. A pending
    DOWNGRADE is deliberately NOT applied here: it takes effect at the next
    boundary, so until then the customer is measured against the limit they
    actually paid for.
    """
    if user.records_limit == -1:
        return False
    return effective_records_used(user, now) >= user.records_limit


def quota_block_reason(user, now: datetime | None = None) -> str | None:
    """Why this user may not start new billable work, or None if they may.

    Returns a caller-facing sentence, and distinguishes the two reasons on
    purpose. "Over your limit" tells a delinquent subscriber to upgrade, which
    does not fix a failed payment; "payment required" tells them the truth.
    """
    if is_frozen(user, now):
        return (
            "Your subscription payment could not be completed, so new scrapes "
            "are paused. Update your payment method to resume — your data and "
            "past exports are untouched."
        )
    if is_over_record_limit(user, now):
        _, end = effective_window(user, now)
        # ISO date, not a locale-formatted one: %-d is not portable off glibc
        # and this string is read by the API, the worker logs and the frontend
        # alike. The window END is the reset moment — the boundary instant
        # belongs to the NEW window.
        return (
            f"Record limit reached "
            f"({effective_records_used(user, now)}/{user.records_limit}). "
            f"Your quota resets {as_utc(end).date().isoformat()} (UTC). "
            "Upgrade your plan to continue now."
        )
    return None
