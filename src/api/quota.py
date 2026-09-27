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

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from src.api.quota_window import as_utc, effective_window, is_frozen, should_roll

__all__ = [
    "RunEligibility",
    "current_period_start",
    "effective_records_limit",
    "effective_records_used",
    "effective_window",
    "is_frozen",
    "is_over_record_limit",
    "next_quota_reset",
    "quota_block_reason",
    "run_eligibility",
]

# The prose every enqueue gate returns in its 402. Callers and the frontend
# show it verbatim, so it changes only on purpose.
FROZEN_MESSAGE = (
    "Your subscription payment could not be completed, so new scrapes are "
    "paused. Update your payment method to resume. Your data and past exports "
    "are untouched."
)
ENDED_MESSAGE = (
    "Your subscription has ended, so new scrapes are paused. Resubscribe to "
    "continue. Your data and past exports are untouched."
)


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


def effective_records_limit(user, now: datetime | None = None) -> int:
    """The limit that will apply once the window this operation charges is open.

    Normally just ``records_limit``. But a DOWNGRADE parked in
    ``pending_records_limit`` is applied BY the rollover, in the same statement
    that zeroes the counter — so a caller deciding anything on the far side of a
    boundary must ask for the post-rollover limit, not the current one.

    The case that made this necessary: an Agency subscriber (``records_limit ==
    -1``, unlimited) with a pending downgrade to Pro. The worker's cap block is
    skipped entirely for unlimited users, so a job starting AFTER their window
    ended would export every lead uncapped — and settlement would then roll the
    window, apply the Pro limit, and leave them at 5000/1000. Asking for the
    effective limit makes the cap block run and reserve against 1,000. (Codex)

    Pending is only ever a downgrade (upgrades apply immediately), so this can
    only ever tighten a limit, never loosen one.
    """
    if should_roll(user, now):
        pending = getattr(user, "pending_records_limit", None)
        if pending is not None:
            return int(pending)
    return user.records_limit


def is_over_record_limit(user, now: datetime | None = None) -> bool:
    """True when the user has consumed their plan's records for THIS window.

    ``-1`` means unlimited and is never over. Inside a live window the customer
    is measured against the limit they actually paid for; a pending downgrade
    only binds once the boundary they are being measured across has passed (see
    ``effective_records_limit``).
    """
    limit = effective_records_limit(user, now)
    if limit == -1:
        return False
    return effective_records_used(user, now) >= limit


def next_quota_reset(user, now: datetime | None = None) -> datetime | None:
    """When this user's record quota next resets, or None if it will not.

    Normally the end of the effective window: the boundary instant belongs to
    the NEW window. But when paid access stops at or before that end
    (``entitlement_ends_at``, a cancel-at-period-end), the window does not
    advance there — ``should_roll`` refuses, there is no entitlement left to
    open a new window against — and the account becomes ``ended`` instead.
    Reporting the window end as a reset date would promise a quota that never
    comes back.

    A FROZEN account gets None for the same reason: its window does not
    advance while payment has failed, so the stored end may already be in the
    past and no reset will happen until the customer pays. (Codex)
    """
    if is_frozen(user, now):
        return None
    _, end = effective_window(user, now)
    end = as_utc(end)
    ends_at = getattr(user, "entitlement_ends_at", None)
    if ends_at is not None and as_utc(ends_at) <= end:
        return None
    return end


@dataclass(frozen=True)
class RunEligibility:
    """Whether an account may start billable work, and if not, why.

    ``resumes_at`` is set only when the block lifts by itself at a known
    instant — an ``over_limit`` account whose quota will reset. ``frozen`` and
    ``ended`` need the customer to act, so they never carry one.
    """

    can_run: bool
    code: Literal["frozen", "ended", "over_limit"] | None = None
    message: str | None = None
    resumes_at: datetime | None = None


def run_eligibility(user, now: datetime | None = None) -> RunEligibility:
    """The account-level rule for starting billable work — the ONE statement of it.

    Every enqueue gate (through ``quota_block_reason``) and ``/billing/usage``
    read this, so the page cannot disagree with the gate. The order matters:
    a frozen account is told "payment required" even when it is also over its
    limit, because "upgrade" does not fix a failed payment.

    ``now`` is read once and handed to every helper, so a call that straddles a
    window boundary cannot mix the old window's usage with the new one's limit.
    """
    now = as_utc(now or datetime.now(UTC))
    if is_frozen(user, now):
        return RunEligibility(False, "frozen", FROZEN_MESSAGE)
    # Paid access that has ALREADY ENDED. The window stops advancing at
    # entitlement_ends_at, but the counter it leaves behind may still have room,
    # so without this check a cancelled customer could keep spending their final
    # window's remainder in the gap between the term ending and either
    # customer.subscription.deleted arriving or the hourly reconciliation
    # downgrading them. Both of those CLEAR the field, so this only ever fires
    # inside that gap — and it is the gap that a lost webhook makes unbounded.
    # (Codex)
    ends_at = getattr(user, "entitlement_ends_at", None)
    if ends_at is not None and now >= as_utc(ends_at):
        return RunEligibility(False, "ended", ENDED_MESSAGE)
    if is_over_record_limit(user, now):
        reset = next_quota_reset(user, now)
        # ISO dates, not locale-formatted ones: %-d is not portable off glibc
        # and this string is read by the API, the worker logs and the frontend
        # alike.
        usage = (
            f"Record limit reached "
            f"({effective_records_used(user, now)}/"
            f"{effective_records_limit(user, now)}). "
        )
        if reset is None:
            # The term ends at or before the window would have reset, so there
            # is no reset to wait for.
            message = (
                f"{usage}Your subscription ends "
                f"{as_utc(ends_at).date().isoformat()} (UTC), so this quota will "
                "not reset. Renew or upgrade your plan to continue."
            )
        else:
            message = (
                f"{usage}Your quota resets {reset.date().isoformat()} (UTC). "
                "Upgrade your plan to continue now."
            )
        return RunEligibility(False, "over_limit", message, reset)
    return RunEligibility(True)


def quota_block_reason(user, now: datetime | None = None) -> str | None:
    """Why this user may not start new billable work, or None if they may.

    A thin reading of ``run_eligibility`` for the enqueue gates, which only
    need the caller-facing sentence.
    """
    eligibility = run_eligibility(user, now)
    return None if eligibility.can_run else eligibility.message
