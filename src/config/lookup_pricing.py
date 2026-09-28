"""The ONE price a contact lookup is quoted at, and the allowance left against it.

Phase 1b-1c (tasks/todo-lookup-contacts.md, finding 15-17). The quote snapshots these
onto the action it becomes (`contact_lookup_actions.unit_price_cents`, `currency`,
`pricing_version`) so a drift between what the customer was SHOWN and what Stripe
invoiced is detectable. Billing never derives from them: the metered Stripe price is
the charge.

`PRICING_VERSION` names the live Stripe metered prices read on 2026-09-28: every
`STRIPE_PRICE_SKIP_TRACE_*` is `usd`, `per_unit`, 8 cents for Pro and Business, 5 for
Agency, monthly and annual alike. Change the numbers and the version together.

`/billing/skip-trace-usage` (`src/api/routes/billing.py`) still carries its own copy
of these rates; moving it here is a logged follow-up, and a test fails if the two
ever disagree.
"""
from __future__ import annotations

from datetime import datetime

from src.config import settings
from src.config.constants import normalize_plan

UNIT_PRICE_CENTS: dict[str, int] = {"pro": 8, "business": 8, "agency": 5}
CURRENCY = "USD"
PRICING_VERSION = "2026-06"


def unit_price_cents(plan: str | None) -> int | None:
    """Cents per lookup past the included allowance; None when the plan has no
    lookups to sell (Starter, or anything unknown)."""
    return UNIT_PRICE_CENTS.get(normalize_plan(plan))


def included_lookups_remaining(user, now: datetime) -> int:
    """Included lookups left in the user's current entitlement window.

    The same rule the billed path applies (`report_lookups_for_user`,
    src/api/billing/skip_trace_usage.py): the counter counts as 0 once the window it
    belongs to has rolled, i.e. `skip_trace_period_start` is unset or earlier than
    the EFFECTIVE window's start. Reading the stored counter alone would under-report
    the allowance between a window ending and the rollover catching up.
    """
    from src.api.quota_window import effective_window

    quota = settings.SKIP_TRACE_BUNDLED_QUOTAS.get(normalize_plan(user.plan), 0)
    used = user.skip_trace_used_this_month or 0
    period_start = user.skip_trace_period_start
    window_start, _window_end = effective_window(user, now)
    if period_start is None or (window_start is not None and period_start < window_start):
        used = 0
    return max(0, quota - used)


__all__ = [
    "CURRENCY", "PRICING_VERSION", "UNIT_PRICE_CENTS",
    "included_lookups_remaining", "unit_price_cents",
]
