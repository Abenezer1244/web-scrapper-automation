"""Effective record-quota usage — the one place the billing period is applied.

``users.records_used`` is only meaningful for the period named by
``users.records_period_start``. Reading the raw column is therefore wrong
whenever that period is stale: the number belongs to a PREVIOUS month and the
user is actually at 0 for the current one.

Staleness is a normal, expected state, not a corruption. The rollover beat task
runs daily rather than exactly at the month boundary (so a Beat outage on the
1st cannot skip a whole month), and the billing path rolls a user's period
forward only when they are actually charged. So between the true month boundary
and whichever comes first — the user's next bill or the next beat run — a
perfectly healthy user can sit on last month's period.

During that window the enforcement gates used to read the raw counter and
reject the user as over quota on usage they no longer owed. This module is the
single expression of the rule, so a gate cannot drift from what the worker
actually bills.
"""

from __future__ import annotations

from datetime import UTC, datetime


def current_period_start(now: datetime | None = None) -> datetime:
    """First instant of the current UTC month — the billing-period boundary."""
    now = now or datetime.now(UTC)
    return now.astimezone(UTC).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )


def effective_records_used(user, now: datetime | None = None) -> int:
    """``user.records_used``, or 0 when its period has not been rolled over yet.

    Mirrors the CASE in the worker's billing statement
    (``src/workers/tasks.py``). Keep the two in step.
    """
    used = user.records_used or 0
    period_start = getattr(user, "records_period_start", None)
    if period_start is None:
        # Unreachable after migration 086. Treat it as current rather than as a
        # free reset: handing out quota is the destructive direction, and the
        # rollover raises an ops alert for any NULL it finds.
        return used
    if period_start.tzinfo is None:
        period_start = period_start.replace(tzinfo=UTC)
    return 0 if period_start < current_period_start(now) else used


def is_over_record_limit(user, now: datetime | None = None) -> bool:
    """True when the user has consumed their plan's records for THIS period.

    ``records_limit == -1`` means unlimited and is never over.
    """
    if user.records_limit == -1:
        return False
    return effective_records_used(user, now) >= user.records_limit
