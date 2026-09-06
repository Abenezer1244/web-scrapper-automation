"""Entitlement windows — the ONE definition of "which month is this user in".

Record quota used to reset on the calendar month while Stripe renewed on the
subscription anniversary. Those are two unrelated clocks, and the gap between
them handed a first-cycle subscriber up to 2x their plan quota on one payment
while giving a converting trial user nothing at all until the 1st.

This module replaces the calendar rule with an **entitlement window**: a
half-open interval ``[quota_period_start, quota_period_end)`` on a monthly grid
anchored at ``users.quota_anchor_at``. The window is always ONE MONTH long
regardless of how Stripe invoices — an annual Pro subscriber gets twelve monthly
windows, not one 1,000-record year.

Three properties this file exists to guarantee:

1. **One definition.** Before this change the period rule was written out SEVEN
   times (the API gate, the reservation SQL, the settlement SQL,
   ``_reservation_is_current``, ``release_quota_reservation``, the beat, and the
   ``/usage`` reader), each with its own ``date_trunc('month', ...)``. Any two
   of them drifting is a silent quota bug. Now the SQL sites all call the
   ``public.quota_*`` functions installed by migration 088 and the Python sites
   all call this module, and ``tests/test_quota_window.py`` proves the two agree
   over a generated date matrix.

2. **Real calendar months, never +30 days.** Boundaries are always computed by
   adding whole months to the ORIGINAL anchor, never to the previous (possibly
   clamped) boundary. An anchor of Jan 31 therefore walks
   Jan 31 -> Feb 28 -> Mar 31 -> Apr 30 -> May 31; February does not
   permanently drag the anchor back to the 28th. Leap years fall out of
   ``calendar.monthrange`` / Postgres month addition, which agree.

3. **UTC only.** Every value is timezone-aware UTC and every SQL boundary is
   re-cast ``AT TIME ZONE 'UTC'``. Postgres month arithmetic on a ``timestamptz``
   is evaluated in the SESSION timezone, so a worker connecting under a negative
   offset would otherwise land on a different day — the same class of bug that
   made a healthy user read as stale in the #223 fix. The browser's timezone
   never enters quota at all.

The rollover rule (``next_window``) is deliberately NOT "start = the grid cell
containing now". A cell can only be entered from the end of the previous window,
so windows are contiguous by construction — no gaps, no overlaps, and no user
can be handed two buckets a day apart when their anchor moves. See
``next_window`` for why the transitional cell uses the CLOSEST boundary.
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime

#: Stripe subscription statuses that mean "not entitled, and not merely late".
#: A user in one of these gets no fresh quota: their window stops advancing
#: until the subscription recovers. ``past_due`` is deliberately NOT here — it
#: is served through a grace period and only freezes afterwards (see
#: ``is_frozen``). NULL status (Starter, free, admin-granted, never-subscribed)
#: is never frozen: those accounts have no subscription to fail.
FROZEN_STATUSES: frozenset[str] = frozenset(
    {"unpaid", "incomplete", "incomplete_expired", "paused"}
)


def as_utc(value: datetime) -> datetime:
    """Coerce a datetime to timezone-aware UTC.

    SQLAlchemy hands back naive datetimes from some drivers/columns even when
    the column is ``timestamptz``. Treating a naive value as local time would
    shift every boundary, so naive is interpreted as UTC — which is what every
    writer in this codebase actually stores.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def add_months(anchor: datetime, k: int) -> datetime:
    """``anchor`` plus ``k`` whole calendar months, clamping the day.

    Mirrors Postgres ``timestamp + interval 'k months'`` exactly: the month
    field advances, the day clamps to the last day of the target month, and the
    time-of-day is preserved. Always call this with the ORIGINAL anchor — adding
    one month repeatedly to a clamped result compounds the clamp and walks
    Jan 31 -> Feb 28 -> Mar 28, which is the bug this signature exists to make
    hard to write.
    """
    anchor = as_utc(anchor)
    total = anchor.month - 1 + k
    year = anchor.year + total // 12
    month = total % 12 + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day)


def grid_index(anchor: datetime, at: datetime) -> int:
    """Largest ``k >= 0`` with ``add_months(anchor, k) <= at``.

    The month difference is only an ESTIMATE: clamping makes a cell shorter than
    a calendar month (Jan 31 -> Feb 28 is 28 days), so the naive estimate can be
    off by one in either direction. Both corrections below are loops rather than
    a single step because being exactly right here is cheap and reasoning about
    "can it ever be off by two?" is not.

    COST NOTE (security review, Low, accepted): the SQL twin searches a
    ``generate_series`` sized by the months between the anchor and ``at``, so a
    ``quota_anchor_at`` far in the past makes every quota statement scan a
    proportionally longer series. Not attacker-reachable — both arguments come
    from our own columns and a bound clock, and the column is NOT NULL with a
    current-month default — and the only writers are the migration backfill
    (``records_period_start``, i.e. this year) and a Stripe
    ``billing_cycle_anchor``. Left unbounded on purpose: a cap would return a
    WRONG index for a genuinely old anchor, which is worse than a slow one.
    """
    anchor, at = as_utc(anchor), as_utc(at)
    if at <= anchor:
        return 0
    k = max(0, (at.year - anchor.year) * 12 + (at.month - anchor.month))
    while k > 0 and add_months(anchor, k) > at:
        k -= 1
    while add_months(anchor, k + 1) <= at:
        k += 1
    return k


def window_containing(anchor: datetime, at: datetime) -> tuple[datetime, datetime]:
    """The grid cell ``[start, end)`` that contains ``at``."""
    k = grid_index(anchor, at)
    return add_months(anchor, k), add_months(anchor, k + 1)


def transitional_end(anchor: datetime, old_end: datetime) -> datetime:
    """End of the window that begins at ``old_end``, snapping back onto the grid.

    In steady state ``old_end`` IS a grid boundary, so ``old_end + 1 month`` IS
    the next boundary and this returns it exactly — no special case, no drift.

    The clause only does real work on the FIRST rollover after an anchor moves
    (a trial converting to paid, or the one-time backfill of a subscriber's real
    Stripe anniversary). There, naively continuing to the next boundary can
    produce an absurd window: a user rolling out of a legacy calendar window
    ending Oct 1 whose new anchor is the 2nd would get ``[Oct 1, Oct 2)`` — a
    one-day cell with a full bucket, immediately followed by another. Picking the
    boundary CLOSEST to ``old_end + 1 month`` instead keeps every transitional
    window within about a fortnight of a month in either direction, converges
    onto the grid in exactly one cell, and can never mint two buckets in quick
    succession. Ties go to the later boundary, and the result is forced past
    ``old_end`` so a window can never be empty or backwards.
    """
    anchor, old_end = as_utc(anchor), as_utc(old_end)
    target = add_months(old_end, 1)
    k = grid_index(anchor, target)
    b_lo = add_months(anchor, k)
    b_hi = add_months(anchor, k + 1)
    end = b_hi if (b_hi - target) <= (target - b_lo) else b_lo
    return end if end > old_end else b_hi


def next_window(
    anchor: datetime, old_end: datetime, at: datetime
) -> tuple[datetime, datetime]:
    """The window a user should be in at ``at``, given they were in one ending
    at ``old_end``.

    Contiguous by construction: the new window starts where the old one ended.
    A user who disappears for three months and comes back does NOT accumulate
    three buckets — the second branch jumps straight to the cell containing
    ``at`` and the counter is zeroed exactly once, wherever it lands.
    """
    anchor, old_end, at = as_utc(anchor), as_utc(old_end), as_utc(at)
    end = transitional_end(anchor, old_end)
    if at < end:
        return old_end, end
    return window_containing(anchor, at)


def is_frozen(user, now: datetime | None = None) -> bool:
    """True when the account may not consume quota because payment has failed.

    Distinct from "over quota": a frozen account is refused with a payment
    message and, critically, its window does NOT advance — so a delinquent
    subscription cannot quietly accrue a fresh bucket every month while unpaid.

    ``past_due`` is served through a grace period (Stripe's own dunning retries
    span several days, and freezing a customer whose card succeeds on retry 3
    would be a self-inflicted outage). Everything in ``FROZEN_STATUSES`` freezes
    at once. No customer data is ever deleted by this state.
    """
    now = as_utc(now or datetime.now(UTC))
    status = (getattr(user, "subscription_status", None) or "").strip()
    if status in FROZEN_STATUSES:
        return True
    if status == "past_due":
        grace = getattr(user, "entitlement_grace_ends_at", None)
        if grace is not None and now >= as_utc(grace):
            return True
    return False


def should_roll(user, now: datetime | None = None) -> bool:
    """True when this user's entitlement window has ended and may be advanced.

    Mirrors ``public.quota_should_roll`` (migration 088). Three reasons NOT to
    roll a window that has ended:

      * the account is frozen for non-payment — advancing would hand a
        non-paying subscription free quota every month;
      * paid access is scheduled to stop (``entitlement_ends_at``) and the
        expired window already reaches it, so there is no entitlement left to
        open a new window against. The hourly reconciliation downgrades such a
        user to Starter and clears the field, which releases the window again —
        that is what stops a lost ``subscription.deleted`` webhook stranding
        someone frozen forever;
      * it simply has not ended yet.
    """
    now = as_utc(now or datetime.now(UTC))
    end = getattr(user, "quota_period_end", None)
    if end is None or as_utc(end) > now:
        return False
    if is_frozen(user, now):
        return False
    ends_at = getattr(user, "entitlement_ends_at", None)
    if ends_at is not None and as_utc(end) >= as_utc(ends_at):
        return False
    return True


def effective_window(user, now: datetime | None = None) -> tuple[datetime, datetime]:
    """The window this user is effectively in RIGHT NOW, without writing.

    Read-only mirror of what the next authoritative quota operation will persist.
    Used by ``/billing/usage`` so the reported window and reset date are never
    the stale stored pair a lazy rollover has not caught up with yet.
    """
    now = as_utc(now or datetime.now(UTC))
    start = as_utc(user.quota_period_start)
    end = as_utc(user.quota_period_end)
    if not should_roll(user, now):
        return start, end
    return next_window(as_utc(user.quota_anchor_at), end, now)


# ── SQL builders — the same rule, for the statements that must be atomic ──────
#
# The rollover cannot be a read-then-write: it has to happen in the SAME
# statement that charges, or a concurrent worker can interleave between the two
# and either lose usage or hand out a second bucket. So the worker paths cannot
# call the Python functions above — they call the ``public.quota_*`` functions
# installed by migration 088, through these builders, so there is still only one
# place per language where the rule is written down.
#
# ``prefix`` is the table alias the users columns are reached through ("u." in a
# join, "" for a bare UPDATE). ``at`` is the bound parameter holding the single
# clock reading the caller pinned for the whole operation — always
# ``clock_timestamp()`` captured once, never NOW(), which is transaction-start
# time and can sit on the wrong side of a boundary by minutes.


def roll_predicate_sql(prefix: str = "", at: str = ":at") -> str:
    """SQL boolean: has this user's window ended AND may it be advanced?"""
    return (
        f"public.quota_should_roll({prefix}quota_period_end, "
        f"{prefix}subscription_status, {prefix}entitlement_grace_ends_at, "
        f"{prefix}entitlement_ends_at, CAST({at} AS timestamptz))"
    )


def next_start_sql(prefix: str = "", at: str = ":at") -> str:
    return (
        f"public.quota_next_start({prefix}quota_anchor_at, "
        f"{prefix}quota_period_end, CAST({at} AS timestamptz))"
    )


def next_end_sql(prefix: str = "", at: str = ":at") -> str:
    return (
        f"public.quota_next_end({prefix}quota_anchor_at, "
        f"{prefix}quota_period_end, CAST({at} AS timestamptz))"
    )


def window_cte_sql(prefix: str = "", at: str = ":at") -> str:
    """The projection every charging statement shares.

    Emits ``rolling``, ``base``, ``eff_limit``, ``new_start`` and ``new_end``
    from a CTE that has already SELECTed and locked the users row. Three things
    happen together here on purpose:

      * the counter is zeroed exactly when the window advances, so a stale
        window can never be read as usage the customer still owes;
      * a DOWNGRADE parked in ``pending_records_limit`` is applied AT the
        boundary and not before, which is what stops a customer who paid for
        Business being cut to a Pro cap mid-window while sitting above it;
      * the new limit is what the grant is computed against, so the boundary
        cannot leak one job's worth of the higher cap into the new window.
    """
    roll = roll_predicate_sql(prefix, at)
    return (
        f"{roll} AS rolling, "
        f"CASE WHEN {roll} THEN 0 ELSE {prefix}records_used END AS base, "
        f"CASE WHEN {roll} THEN COALESCE({prefix}pending_records_limit, "
        f"{prefix}records_limit) ELSE {prefix}records_limit END AS eff_limit, "
        f"CASE WHEN {roll} THEN {next_start_sql(prefix, at)} "
        f"ELSE {prefix}quota_period_start END AS new_start, "
        f"CASE WHEN {roll} THEN {next_end_sql(prefix, at)} "
        f"ELSE {prefix}quota_period_end END AS new_end"
    )


def window_set_sql(cte: str = "w") -> str:
    """The SET clause every charging statement shares.

    Applied FROM a CTE (aliased ``cte``) carrying ``window_cte_sql``'s columns
    plus ``pending_plan``, against the users row aliased ``u``.

    ``records_period_start`` is written in lockstep with ``quota_period_start``
    for one release. It is now a MIRROR, not a source of truth, but the
    skip-trace half of the beat, ``scripts/cleanup_watchdog_billed_dups.py`` and
    operator queries still read it — letting it drift would break them silently.
    It is dropped once those move over.
    """
    c = f"{cte}."
    return (
        f"  records_limit = {c}eff_limit, "
        f"  plan = CASE WHEN {c}rolling AND {c}pending_plan IS NOT NULL "
        f"              THEN {c}pending_plan ELSE u.plan END, "
        f"  pending_plan = CASE WHEN {c}rolling THEN NULL ELSE u.pending_plan END, "
        f"  pending_records_limit = CASE WHEN {c}rolling THEN NULL "
        f"                               ELSE u.pending_records_limit END, "
        f"  quota_period_start = {c}new_start, "
        f"  quota_period_end = {c}new_end, "
        f"  records_period_start = {c}new_start"
    )


def reservation_is_current_sql(
    *,
    job_window: str,
    job_reserved_at: str,
    user_window: str,
    user_records_period_start: str,
    rolling: str = "rolling",
) -> str:
    """Does this job's quota reservation still belong to the LIVE window?

    Settlement nets ``billable - reserved`` only when it does. If the window has
    since rolled, the reserved amount was zeroed along with it, so netting it
    off would deliver records charged to nobody — the leads are being delivered
    now, so the live window must carry them in full. Release has the mirror
    obligation: refunding into a window the grant was never added to would
    destroy current-window usage.

    The old form of this test compared calendar MONTHS, which is only
    accidentally correct while every window starts on the 1st: with a 20th
    anchor, a job reserving on the 19th and settling on the 21st read as "same
    period" and silently gave the records away.

    ``rolling`` is the caller's own rollover predicate. It is part of the test
    because the stored window can have ENDED without rolling — a frozen account
    does not advance, so its counter still holds the grant and netting is right.

    A NULL ``job_window`` means the job reserved BEFORE migration 088, so it
    falls back to the month comparison it was written under. That is exactly
    right for those rows (every window then WAS a calendar month) and the set is
    finite: it drains as the in-flight jobs at deploy time finish.

    Callers pass column EXPRESSIONS, not table prefixes, because the two call
    sites reach these values through different shapes — one through a CTE that
    has already renamed them, one through a correlated UPDATE ... FROM.
    """
    return (
        f"(CASE WHEN {job_window} IS NOT NULL "
        f"      THEN {job_window} = {user_window} AND NOT {rolling} "
        f"      ELSE date_trunc('month', {user_records_period_start} AT TIME ZONE 'UTC') "
        f"           = date_trunc('month', {job_reserved_at} AT TIME ZONE 'UTC') "
        f" END)"
    )
