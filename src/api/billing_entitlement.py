"""Subscription lifecycle -> entitlement state. The nine approved policies, once.

The Stripe webhook handlers used to write ``plan`` and ``records_limit`` inline
and nothing else, which is why every lifecycle question had a wrong answer:
converting from a consumed trial gave the customer nothing until the 1st, a
mid-cycle downgrade could strand someone at 3000/1000, and ``past_due`` kept
minting fresh quota until the subscription was finally deleted.

The transitions now live here, as functions that take a ``User`` and the Stripe
objects and mutate the user. Keeping them out of the route makes them testable
without HTTP or a Stripe signature, and — more importantly — puts all nine
policies where they can be read together.

THE INVARIANT THAT MAKES THIS SAFE

**Plan and status changes never move the anchor or the window.** The entitlement
anchor moves on exactly three events:

  1. the FIRST trial -> paid conversion,
  2. a resubscribe after the previous paid entitlement genuinely LAPSED,
  3. explicit admin action (not a webhook path).

Upgrade, downgrade, cancel-at-period-end, dunning, payment recovery and
monthly<->annual switches all leave the window exactly where it is. That single
rule is what makes upgrade-farming and cancel/resubscribe-farming worthless:
there is no webhook a customer can trigger that hands them a second bucket
inside one entitlement month.

IDEMPOTENCY

Stripe retries for three days and can deliver out of order. Redis dedup in the
route stops an identical event being processed twice, but two DIFFERENT events
can describe the same conversion. So the one destructive action here — zeroing
the counter on conversion — is gated on durable state (``first_paid_at`` /
``paid_entitlement_ended_at``) rather than on the event, and every function is
safe to run repeatedly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.api.quota_window import as_utc, window_containing
from src.config import settings
from src.db.models import User
from src.utils.logger import setup_logger

_logger = setup_logger("api.billing_entitlement")

#: Stripe statuses under which the customer is entitled to paid service.
#: ``past_due`` is here because dunning is still in progress — the freeze it
#: eventually earns is time-based (``entitlement_grace_ends_at``), not status-
#: based, so that a card which succeeds on retry 3 never causes an outage.
ENTITLED_STATUSES = ("active", "trialing", "past_due")


def _rank(records_limit: int) -> float:
    """Order plan sizes so unlimited sorts ABOVE every finite plan.

    ``-1`` means unlimited. Comparing it numerically would make Agency look
    smaller than Starter and turn every upgrade to Agency into a deferred
    "downgrade" that never takes effect.
    """
    return float("inf") if records_limit == -1 else float(records_limit)


def _start_fresh_paid_window(user: User, anchor: datetime | None, now: datetime) -> None:
    """Re-anchor the monthly grid and open a fresh window with a zero counter.

    Called on exactly the two customer-triggered events approved for it: a first
    trial -> paid conversion, and a resubscribe after the previous entitlement
    lapsed. Nothing else in this module calls it.

    The anchor is Stripe's ``billing_cycle_anchor`` — the stable recurring anchor
    that survives plan changes and is the ANNUAL anniversary for annual prices,
    which is exactly what we want: the day-of-month drives a MONTHLY grid, so an
    annual Pro subscriber gets twelve windows a year rather than 1,000 records
    for the year.

    An anchor in the FUTURE (a Stripe-side trial defers the first invoice) is
    clamped to now. Otherwise the customer's window would start after the moment
    they began using the product, and they would be metered against a window
    they are not yet inside.
    """
    anchor = as_utc(anchor) if anchor else now
    if anchor > now:
        anchor = now
    start, end = window_containing(anchor, now)
    user.quota_anchor_at = anchor
    user.quota_period_start = start
    user.quota_period_end = end
    user.records_used = 0
    # records_period_start is a MIRROR of the window start for one release (the
    # skip-trace beat and operator queries still read it). Keeping it in step
    # here is the same lockstep the worker's charging statements maintain.
    user.records_period_start = start
    user.paid_entitlement_ended_at = None


def _clear_dunning(user: User) -> None:
    user.entitlement_grace_ends_at = None


def mark_payment_failed(user: User, *, now: datetime | None = None) -> datetime:
    """P7 — start (but never extend) the dunning grace. Returns its end.

    Stripe retries a failed invoice over several days. Freezing on the first
    failure would cut off a customer whose card succeeds on retry 3; serving
    forever would hand a non-paying subscription a fresh bucket every month. So
    the account is served normally until the grace expires, after which
    ``quota_window.is_frozen`` refuses new billable work AND the rollover stops
    — no data is deleted, and past exports stay available.

    The grace is set only when it is currently unset, so a second failed invoice
    (or a webhook replay) cannot roll the deadline forward indefinitely. That
    idempotency is the whole anti-leak property.
    """
    now = as_utc(now or datetime.now(UTC))
    if user.entitlement_grace_ends_at is None:
        user.entitlement_grace_ends_at = now + timedelta(
            days=settings.BILLING_PAST_DUE_GRACE_DAYS
        )
        _logger.info(
            "entitlement: payment failed for user %s — grace until %s",
            user.id, user.entitlement_grace_ends_at,
        )
    return as_utc(user.entitlement_grace_ends_at)


def activate_paid_plan(
    user: User,
    *,
    plan: str,
    records_limit: int,
    subscription_id: str | None,
    status: str | None,
    billing_cycle_anchor: datetime | None,
    now: datetime | None = None,
) -> bool:
    """P1 (trial -> paid) and P9 (resubscribe). Returns True if quota was reset.

    A trial user who consumed 1,000/1,000 and then pays $199 must receive a full
    paid month starting AT CONVERSION. Under the calendar rule they received
    nothing until the 1st — the one gap in the old system that took something
    away from a customer who had paid.

    The reset fires only when durable state says this is genuinely a new paid
    entitlement:

      * ``first_paid_at IS NULL`` — they have never paid before, or
      * ``paid_entitlement_ended_at IS NOT NULL`` — their previous paid
        entitlement was seen to END (a ``customer.subscription.deleted`` we
        processed), so this is a real resubscribe.

    Both are cleared as part of the same call, so a webhook replay, or a second
    event describing the same conversion, finds neither condition true and
    cannot mint a second bucket. Cancelling and resubscribing INSIDE a live
    entitlement never sets ``paid_entitlement_ended_at``, so it grants nothing.

    ``trial_consumed_at`` is stamped permanently: the app trial is a
    once-per-account grant, and ``trial_ends_at`` is cleared here so it cannot
    answer that question later.
    """
    now = as_utc(now or datetime.now(UTC))
    fresh = user.first_paid_at is None or user.paid_entitlement_ended_at is not None

    user.plan = plan
    user.records_limit = records_limit
    user.stripe_subscription_id = subscription_id
    user.subscription_status = status
    # A paying customer is not on the app trial any more, whichever way they got
    # here — expire_trials must never downgrade them.
    if user.trial_consumed_at is None and user.trial_ends_at is not None:
        user.trial_consumed_at = as_utc(user.trial_ends_at)
    user.trial_ends_at = None
    if user.trial_consumed_at is None:
        user.trial_consumed_at = now
    user.entitlement_ends_at = None
    _clear_dunning(user)
    # A pending downgrade cannot survive a new paid entitlement — the customer
    # has just told us what they want to be on.
    user.pending_plan = None
    user.pending_records_limit = None

    if fresh:
        _start_fresh_paid_window(user, billing_cycle_anchor, now)
        _logger.info(
            "entitlement: fresh paid window for user %s (plan=%s) "
            "[%s, %s), records_used reset to 0",
            user.id, plan, user.quota_period_start, user.quota_period_end,
        )
    if user.first_paid_at is None:
        user.first_paid_at = now
    return fresh


def apply_plan_change(
    user: User,
    *,
    plan: str,
    records_limit: int,
    subscription_id: str | None,
    status: str | None,
    cancel_at_period_end: bool,
    entitlement_end: datetime | None,
    billing_cycle_anchor: datetime | None = None,
    now: datetime | None = None,
) -> str:
    """P4 (upgrade), P5 (downgrade), P6a (cancel at period end), P7/P8 status.

    Returns one of ``"upgrade"``, ``"downgrade_pending"``, ``"unchanged"`` or
    ``"converted"`` for logging and tests.

    UPGRADE is immediate and keeps both the window and ``records_used``:
    600/1000 becomes 600/5000. Starting a new window here instead would let a
    customer farm a fresh bucket by upgrading and downgrading repeatedly, which
    Stripe prorations make nearly free.

    DOWNGRADE is deferred to the next entitlement boundary. Applying the smaller
    cap immediately would turn 3000/5000 into 3000/1000 and lock a customer out
    of quota they had already paid for. The target is parked in
    ``pending_plan`` / ``pending_records_limit`` and applied by the rollover —
    the same statement that zeroes the counter, so the new window opens at the
    new cap and not a record earlier.

    CANCEL-AT-PERIOD-END records when paid access stops. The window keeps rolling
    monthly up to that instant and then stops; the reconciliation downgrades the
    account once it passes, which is also what protects a customer whose
    ``subscription.deleted`` webhook is never delivered.
    """
    now = as_utc(now or datetime.now(UTC))

    # A subscription that has become entitled for the first time — or after a
    # genuine lapse — is a CONVERSION, not a plan change. Checkout usually gets
    # there first, but a customer can also reach an entitled state through an
    # update alone (a Stripe-side trial converting, an incomplete payment
    # finally succeeding), and that path must not be the one that forgets to
    # give them their paid month.
    if status in ("active", "trialing") and (
        user.first_paid_at is None or user.paid_entitlement_ended_at is not None
    ):
        activate_paid_plan(
            user,
            plan=plan,
            records_limit=records_limit,
            subscription_id=subscription_id,
            status=status,
            billing_cycle_anchor=billing_cycle_anchor,
            now=now,
        )
        return "converted"

    user.stripe_subscription_id = subscription_id
    user.subscription_status = status
    if status in ("active", "trialing"):
        # Whatever dunning was in flight is over.
        _clear_dunning(user)
        user.trial_ends_at = None
    elif status == "past_due":
        # P7 belt. invoice.payment_failed normally starts the grace, but it can
        # be delayed, lost, or arrive before checkout has bound
        # stripe_subscription_id — and a past_due account with a NULL grace is
        # NOT frozen, so its window would keep rolling and hand a non-paying
        # subscription a fresh bucket every month. Whichever event observes
        # past_due first starts the clock; the "only when NULL" rule is what
        # keeps the two from extending each other's deadline.
        mark_payment_failed(user, now=now)

    # Scheduled cancellation, or its reversal. Writing None on the reversal is
    # deliberate: a customer who un-cancels must not stay pinned to a stale end
    # date that would silently stop their window from advancing.
    user.entitlement_ends_at = as_utc(entitlement_end) if (
        cancel_at_period_end and entitlement_end
    ) else None

    current = _rank(user.records_limit)
    target = _rank(records_limit)
    if target > current:
        user.plan = plan
        user.records_limit = records_limit
        user.pending_plan = None
        user.pending_records_limit = None
        return "upgrade"
    if target < current:
        user.pending_plan = plan
        user.pending_records_limit = records_limit
        _logger.info(
            "entitlement: downgrade to %s (%s records) deferred to %s for user %s",
            plan, records_limit, user.quota_period_end, user.id,
        )
        return "downgrade_pending"
    # Same size — a monthly<->annual switch, or a repeat event. The quota
    # question is settled, so the plan name is simply brought in line and any
    # parked downgrade is dropped: Stripe now says this is what they are on.
    user.plan = plan
    user.pending_plan = None
    user.pending_records_limit = None
    return "unchanged"


def end_subscription(user: User, *, now: datetime | None = None) -> None:
    """P6b/P6c — the paid term has actually ended (``subscription.deleted``).

    Drops to Starter immediately, which is right for both a term that ran out
    and an immediate cancellation; Stripe sends the same event for each.

    ``records_used``, the window and the anchor are deliberately UNTOUCHED. The
    customer already received those records, so refunding the counter would be a
    small free grant on every cancellation and would break the invariant that no
    plan change ever resets quota. They regain quota at their own next boundary,
    at the Starter cap.

    ``entitlement_ends_at`` is cleared for a reason that is easy to miss: it is
    what stops the window advancing while a cancellation is pending. Leaving it
    set after the downgrade would freeze a now-free account forever.
    """
    now = as_utc(now or datetime.now(UTC))
    user.plan = "starter"
    user.records_limit = settings.PLAN_LIMITS["starter"]
    user.stripe_subscription_id = None
    user.subscription_status = "canceled"
    user.paid_entitlement_ended_at = now
    user.entitlement_ends_at = None
    _clear_dunning(user)
    user.pending_plan = None
    user.pending_records_limit = None


def mark_payment_succeeded(
    user: User, *, status: str | None = None, now: datetime | None = None
) -> None:
    """Payment recovered, or a renewal invoice was paid.

    Deliberately does NOT reset the counter or advance the window. Renewal is
    observed, not acted on: making payment the trigger for fresh quota would
    strand a renewed payer at cap behind a late webhook and would hand out a
    second bucket on a replay. The window advances on its own schedule, lazily,
    from the anchor — which is why a missing ``invoice.payment_succeeded`` can
    never cost a paying customer their month.

    What it DOES do is lift the dunning freeze, and advancing is then automatic:
    the next quota operation (or the hourly reconciliation) sees a window that
    ended while frozen and rolls it to the window containing now — exactly one
    bucket, never one per frozen month.
    """
    now = as_utc(now or datetime.now(UTC))
    _clear_dunning(user)
    if status:
        user.subscription_status = status
