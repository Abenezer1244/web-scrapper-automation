"""The nine approved entitlement policies, and the migration that introduces them.

Quota used to reset on the calendar month while Stripe renewed on the
subscription anniversary. Two unrelated clocks, and every gap between them was a
defect: a first-cycle subscriber crossing a reset received up to 2x their plan
quota on ONE payment, and a trial user who consumed their allowance and then
PAID received nothing until the 1st.

Each test below names the policy it pins:

  P1 trial -> paid          P4 upgrade        P7 past_due
  P2 monthly renewal        P5 downgrade      P8 unpaid
  P3 annual                 P6 cancellation   P9 resubscribe

plus the invariant that ties them together — **plan and status changes never
move the anchor or the window** — which is what makes upgrade-farming and
cancel/resubscribe-farming worthless.

Real Postgres, real settings, no mocks — per the project testing rules.
"""

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from src.api.auth import hash_password
from src.api.billing_entitlement import (
    activate_paid_plan,
    apply_plan_change,
    end_subscription,
    mark_payment_failed,
    mark_payment_succeeded,
)
from src.api.quota import effective_records_used, is_over_record_limit
from src.api.quota_window import (
    add_months,
    as_utc,
    effective_window,
    is_frozen,
    should_roll,
)
from src.config import settings
from src.db.models import User
from src.db.session import SyncSessionLocal

NOW = datetime(2026, 9, 6, 14, 37, tzinfo=UTC)


def _mk_user(db, **kw) -> User:
    """A user in a live entitlement window, with sane defaults for every field
    the lifecycle functions read."""
    start = kw.pop("quota_period_start", NOW - timedelta(days=5))
    defaults = {
        "plan": "pro",
        "records_used": 0,
        "records_limit": 1000,
        "quota_anchor_at": start,
        "quota_period_start": start,
        "quota_period_end": kw.pop("quota_period_end", add_months(start, 1)),
        "records_period_start": start,
        "skip_trace_period_start": start,
    }
    defaults.update(kw)
    user = User(
        id=str(uuid.uuid4()),
        email=f"ent_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"),
        **defaults,
    )
    db.add(user)
    db.flush()
    return user


# ─── P1: trial -> paid ────────────────────────────────────────────────────────

def test_p1_a_consumed_trial_that_converts_gets_a_full_paid_month():
    """The one gap in the old system that took something AWAY from a customer.

    Burn 1,000/1,000 on the trial, pay $199 on day 4, and under the calendar rule
    you received nothing until the 1st. The paid window must start AT CONVERSION
    with a zero counter.
    """
    with SyncSessionLocal() as db:
        user = _mk_user(
            db, records_used=1000, trial_ends_at=NOW + timedelta(days=3)
        )
        assert is_over_record_limit(user, NOW) is True

        anchor = NOW  # Stripe billing_cycle_anchor for a same-day conversion
        reset = activate_paid_plan(
            user, plan="pro", records_limit=1000, subscription_id="sub_1",
            status="active", billing_cycle_anchor=anchor, now=NOW,
        )
        db.flush()

    assert reset is True
    assert user.records_used == 0
    assert is_over_record_limit(user, NOW) is False
    assert user.quota_period_start == NOW
    assert user.quota_period_end == datetime(2026, 10, 6, 14, 37, tzinfo=UTC)
    assert user.trial_ends_at is None
    assert user.trial_consumed_at is not None, "the trial is spent, permanently"
    assert user.first_paid_at == NOW


def test_p1_conversion_is_idempotent_against_a_webhook_replay():
    """Stripe retries for three days and can deliver two events describing ONE
    conversion. The reset is gated on durable state, not on the event, so the
    second delivery must not hand out a second bucket."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, records_used=1000, trial_ends_at=NOW)
        activate_paid_plan(
            user, plan="pro", records_limit=1000, subscription_id="sub_1",
            status="active", billing_cycle_anchor=NOW, now=NOW,
        )
        db.flush()
        later = NOW + timedelta(hours=2)
        user.records_used = 300  # they consumed some of the new month

        reset_again = activate_paid_plan(
            user, plan="pro", records_limit=1000, subscription_id="sub_1",
            status="active", billing_cycle_anchor=NOW, now=later,
        )
        db.flush()

    assert reset_again is False
    assert user.records_used == 300, "a replay must not zero a live counter"
    assert user.first_paid_at == NOW, "the first payment instant is not rewritten"


def test_p1_a_stripe_anchor_in_the_future_is_clamped_to_now():
    """A Stripe-side trial defers the first invoice, so billing_cycle_anchor can
    be days ahead. Anchoring there would start the customer's window AFTER they
    began using the product."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, records_used=900)
        activate_paid_plan(
            user, plan="pro", records_limit=1000, subscription_id="sub_1",
            status="trialing", billing_cycle_anchor=NOW + timedelta(days=10),
            now=NOW,
        )
        db.flush()

    assert user.quota_period_start == NOW
    assert user.quota_period_start <= NOW < user.quota_period_end


# ─── P2: monthly renewal ──────────────────────────────────────────────────────

def test_p2_a_paid_subscriber_crossing_the_1st_gets_nothing_new():
    """The revenue leak the whole change exists to close.

    Anchored on the 20th, a subscriber used to be reset by the calendar task on
    the 1st and charged again on the 20th — up to 2x quota for one payment.
    Crossing the 1st must now be a non-event.
    """
    start = datetime(2026, 8, 20, tzinfo=UTC)
    with SyncSessionLocal() as db:
        user = _mk_user(
            db, records_used=900, quota_period_start=start,
            quota_period_end=datetime(2026, 9, 20, tzinfo=UTC),
            subscription_status="active",
        )
        db.flush()

    on_the_first = datetime(2026, 9, 1, 0, 5, tzinfo=UTC)
    assert should_roll(user, on_the_first) is False
    assert effective_records_used(user, on_the_first) == 900
    assert effective_window(user, on_the_first) == (
        start, datetime(2026, 9, 20, tzinfo=UTC)
    )


def test_p2_the_anniversary_is_what_resets_quota():
    start = datetime(2026, 8, 20, tzinfo=UTC)
    with SyncSessionLocal() as db:
        user = _mk_user(
            db, records_used=900, quota_period_start=start,
            quota_period_end=datetime(2026, 9, 20, tzinfo=UTC),
            subscription_status="active",
        )
        db.flush()

    anniversary = datetime(2026, 9, 20, 0, 1, tzinfo=UTC)
    assert should_roll(user, anniversary) is True
    assert effective_records_used(user, anniversary) == 0
    assert effective_window(user, anniversary) == (
        datetime(2026, 9, 20, tzinfo=UTC), datetime(2026, 10, 20, tzinfo=UTC)
    )


def test_p2_payment_succeeded_never_resets_the_counter():
    """Renewal is OBSERVED, not acted on.

    Making payment the trigger for fresh quota would strand a renewed payer at
    cap behind a late webhook and hand out a second bucket on a replay. The
    window advances on its own, from the anchor.
    """
    with SyncSessionLocal() as db:
        user = _mk_user(db, records_used=640, subscription_status="active")
        before = (user.quota_period_start, user.quota_period_end)
        mark_payment_succeeded(user, status="active", now=NOW)
        db.flush()

    assert user.records_used == 640
    assert (user.quota_period_start, user.quota_period_end) == before


# ─── P3: annual ───────────────────────────────────────────────────────────────

def test_p3_an_annual_subscriber_gets_twelve_monthly_windows():
    """"1,000 records/month" must not become 1,000 records/year.

    The window is always one month; the ANNUAL anniversary only supplies the
    day-of-month that the monthly grid is anchored to.
    """
    anchor = datetime(2026, 3, 20, 9, 0, tzinfo=UTC)  # annual billing anchor
    with SyncSessionLocal() as db:
        user = _mk_user(
            db, quota_period_start=anchor, quota_period_end=add_months(anchor, 1),
            subscription_status="active",
        )
        user.quota_anchor_at = anchor
        db.flush()

    # Walk a full year: every window is one month, all on the 20th at 09:00.
    for k in range(12):
        at = add_months(anchor, k) + timedelta(days=1)
        start, end = effective_window(user, at)
        assert start == add_months(anchor, k)
        assert end == add_months(anchor, k + 1)
        assert start.day == 20 and start.hour == 9
        user.quota_period_start, user.quota_period_end = start, end


# ─── P4: upgrade ──────────────────────────────────────────────────────────────

def test_p4_upgrade_is_immediate_and_keeps_the_window_and_usage():
    """600/1000 becomes 600/5000. Not 0/5000 — resetting here would let a
    customer farm a fresh bucket by upgrading and downgrading repeatedly, which
    Stripe prorations make nearly free."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, plan="pro", records_used=600, records_limit=1000,
                        subscription_status="active", first_paid_at=NOW)
        window = (user.quota_anchor_at, user.quota_period_start, user.quota_period_end)

        outcome = apply_plan_change(
            user, plan="business", records_limit=5000, subscription_id="sub_1",
            status="active", cancel_at_period_end=False, entitlement_end=None,
            now=NOW,
        )
        db.flush()

    assert outcome == "upgrade"
    assert (user.plan, user.records_used, user.records_limit) == ("business", 600, 5000)
    assert (user.quota_anchor_at, user.quota_period_start, user.quota_period_end) == window


def test_p4_upgrading_at_cap_unblocks_immediately():
    with SyncSessionLocal() as db:
        user = _mk_user(db, plan="pro", records_used=1000, records_limit=1000,
                        subscription_status="active", first_paid_at=NOW)
        assert is_over_record_limit(user, NOW) is True
        apply_plan_change(
            user, plan="business", records_limit=5000, subscription_id="sub_1",
            status="active", cancel_at_period_end=False, entitlement_end=None,
            now=NOW,
        )
        db.flush()

    assert is_over_record_limit(user, NOW) is False
    assert user.records_used == 1000, "usage is real; only the cap moved"


def test_p4_upgrade_to_agency_is_an_upgrade_not_a_downgrade():
    """-1 means unlimited. Ranked numerically it would look SMALLER than Starter
    and every upgrade to Agency would become a deferred downgrade that never
    took effect."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, plan="business", records_limit=5000,
                        subscription_status="active", first_paid_at=NOW)
        outcome = apply_plan_change(
            user, plan="agency", records_limit=-1, subscription_id="sub_1",
            status="active", cancel_at_period_end=False, entitlement_end=None,
            now=NOW,
        )
        db.flush()

    assert outcome == "upgrade"
    assert user.records_limit == -1
    assert user.pending_plan is None


# ─── P5: downgrade ────────────────────────────────────────────────────────────

def test_p5_downgrade_is_deferred_so_nobody_is_stranded_over_the_new_cap():
    """3000/5000 must never become 3000/1000 — that locks a customer out of
    quota they already paid for. The change is parked and applied by the
    rollover, in the same statement that zeroes the counter."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, plan="business", records_used=3000, records_limit=5000,
                        subscription_status="active", first_paid_at=NOW)
        outcome = apply_plan_change(
            user, plan="pro", records_limit=1000, subscription_id="sub_1",
            status="active", cancel_at_period_end=False, entitlement_end=None,
            now=NOW,
        )
        db.flush()

    assert outcome == "downgrade_pending"
    assert (user.plan, user.records_limit) == ("business", 5000)
    assert (user.pending_plan, user.pending_records_limit) == ("pro", 1000)
    assert is_over_record_limit(user, NOW) is False


def test_p5_a_reverted_downgrade_clears_the_parked_change():
    with SyncSessionLocal() as db:
        user = _mk_user(db, plan="business", records_limit=5000,
                        subscription_status="active", first_paid_at=NOW)
        apply_plan_change(
            user, plan="pro", records_limit=1000, subscription_id="sub_1",
            status="active", cancel_at_period_end=False, entitlement_end=None,
            now=NOW,
        )
        assert user.pending_plan == "pro"
        # Stripe says they are back on Business before the boundary arrives.
        outcome = apply_plan_change(
            user, plan="business", records_limit=5000, subscription_id="sub_1",
            status="active", cancel_at_period_end=False, entitlement_end=None,
            now=NOW,
        )
        db.flush()

    assert outcome == "unchanged"
    assert user.pending_plan is None
    assert user.records_limit == 5000


def test_p5_a_monthly_to_annual_switch_is_a_no_op_for_quota():
    """Same plan, same allowance, different invoice cadence. It must not move
    the window, reset the counter, or register as a plan change."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, plan="pro", records_used=400, records_limit=1000,
                        subscription_status="active", first_paid_at=NOW)
        window = (user.quota_anchor_at, user.quota_period_start, user.quota_period_end)
        outcome = apply_plan_change(
            user, plan="pro", records_limit=1000, subscription_id="sub_1",
            status="active", cancel_at_period_end=False, entitlement_end=None,
            now=NOW,
        )
        db.flush()

    assert outcome == "unchanged"
    assert user.records_used == 400
    assert (user.quota_anchor_at, user.quota_period_start, user.quota_period_end) == window


# ─── P6: cancellation ─────────────────────────────────────────────────────────

def test_p6a_cancel_at_period_end_keeps_everything_until_the_end():
    with SyncSessionLocal() as db:
        user = _mk_user(db, plan="pro", records_used=200, records_limit=1000,
                        subscription_status="active", first_paid_at=NOW)
        term_end = NOW + timedelta(days=20)
        apply_plan_change(
            user, plan="pro", records_limit=1000, subscription_id="sub_1",
            status="active", cancel_at_period_end=True, entitlement_end=term_end,
            now=NOW,
        )
        db.flush()

    assert user.plan == "pro"
    assert user.records_limit == 1000
    assert user.entitlement_ends_at == term_end
    assert is_frozen(user, NOW) is False, "they paid for this term"


def test_p6a_the_window_stops_at_the_entitlement_end():
    """A cancelling customer keeps what they paid for and not a month more."""
    start = datetime(2026, 9, 1, tzinfo=UTC)
    with SyncSessionLocal() as db:
        user = _mk_user(
            db, quota_period_start=start,
            quota_period_end=datetime(2026, 10, 1, tzinfo=UTC),
            subscription_status="active",
            entitlement_ends_at=datetime(2026, 10, 1, tzinfo=UTC),
        )
        db.flush()

    assert should_roll(user, datetime(2026, 10, 1, 0, 1, tzinfo=UTC)) is False


def test_p6a_un_cancelling_clears_the_end_so_the_window_resumes():
    """A stale end date would silently stop the window advancing forever."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, subscription_status="active", first_paid_at=NOW,
                        entitlement_ends_at=NOW + timedelta(days=10))
        apply_plan_change(
            user, plan="pro", records_limit=1000, subscription_id="sub_1",
            status="active", cancel_at_period_end=False, entitlement_end=None,
            now=NOW,
        )
        db.flush()

    assert user.entitlement_ends_at is None


def test_p6b_deletion_drops_to_starter_without_refunding_the_counter():
    """They already received those records. Resetting here would be a small free
    grant on every cancellation and would break the invariant that no plan change
    ever resets quota."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, plan="pro", records_used=900, records_limit=1000,
                        subscription_status="active", first_paid_at=NOW,
                        stripe_subscription_id="sub_1")
        window = (user.quota_anchor_at, user.quota_period_start, user.quota_period_end)
        end_subscription(user, now=NOW)
        db.flush()

    assert user.plan == "starter"
    assert user.records_limit == settings.PLAN_LIMITS["starter"]
    assert user.records_used == 900, "the counter is not refunded"
    assert (user.quota_anchor_at, user.quota_period_start, user.quota_period_end) == window
    assert user.stripe_subscription_id is None
    assert user.paid_entitlement_ended_at == NOW
    assert user.entitlement_ends_at is None, (
        "must be cleared, or the now-free account is frozen forever"
    )


def test_p6c_immediate_cancellation_takes_the_same_path():
    """Stripe sends customer.subscription.deleted for both, so one handler
    covers a term that ran out and a cancel-right-now."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, plan="business", records_used=10, records_limit=5000,
                        subscription_status="active", first_paid_at=NOW,
                        entitlement_ends_at=NOW + timedelta(days=25))
        end_subscription(user, now=NOW)
        db.flush()

    assert user.plan == "starter"
    assert user.entitlement_ends_at is None
    assert user.paid_entitlement_ended_at == NOW


# ─── P7: past_due ─────────────────────────────────────────────────────────────

def test_p7_past_due_is_served_through_the_grace_then_frozen():
    with SyncSessionLocal() as db:
        user = _mk_user(db, subscription_status="active", first_paid_at=NOW)
        grace_end = mark_payment_failed(user, now=NOW)
        user.subscription_status = "past_due"
        db.flush()

    assert grace_end == NOW + timedelta(days=settings.BILLING_PAST_DUE_GRACE_DAYS)
    assert is_frozen(user, NOW + timedelta(days=1)) is False, "retries take days"
    assert is_frozen(user, grace_end + timedelta(seconds=1)) is True


def test_p7_a_second_failed_invoice_does_not_extend_the_grace():
    """The whole anti-leak property: without this a delinquent subscription could
    push the deadline out forever, one failed invoice at a time."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, subscription_status="past_due", first_paid_at=NOW)
        first = mark_payment_failed(user, now=NOW)
        again = mark_payment_failed(user, now=NOW + timedelta(days=3))
        db.flush()

    assert first == again


def test_p7_past_due_seen_only_via_subscription_updated_still_starts_the_grace():
    """A belt that closes a real leak.

    invoice.payment_failed normally starts the dunning clock, but it can be
    delayed, lost, or arrive before checkout has bound stripe_subscription_id —
    and a past_due account with a NULL grace is NOT frozen, so its window would
    keep rolling and hand a non-paying subscription a fresh bucket every month.
    Whichever event observes past_due first must start the clock.
    """
    with SyncSessionLocal() as db:
        user = _mk_user(db, subscription_status="active", first_paid_at=NOW)
        assert user.entitlement_grace_ends_at is None
        apply_plan_change(
            user, plan="pro", records_limit=1000, subscription_id="sub_1",
            status="past_due", cancel_at_period_end=False, entitlement_end=None,
            now=NOW,
        )
        db.flush()

    assert user.entitlement_grace_ends_at == NOW + timedelta(
        days=settings.BILLING_PAST_DUE_GRACE_DAYS
    )
    assert is_frozen(user, NOW) is False, "still inside the grace"
    assert is_frozen(user, user.entitlement_grace_ends_at + timedelta(seconds=1)) is True


def test_p7_the_two_past_due_paths_do_not_extend_each_others_deadline():
    """Both events observing the same dunning must not push the deadline out."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, subscription_status="active", first_paid_at=NOW)
        apply_plan_change(
            user, plan="pro", records_limit=1000, subscription_id="sub_1",
            status="past_due", cancel_at_period_end=False, entitlement_end=None,
            now=NOW,
        )
        first = user.entitlement_grace_ends_at
        mark_payment_failed(user, now=NOW + timedelta(days=2))
        apply_plan_change(
            user, plan="pro", records_limit=1000, subscription_id="sub_1",
            status="past_due", cancel_at_period_end=False, entitlement_end=None,
            now=NOW + timedelta(days=4),
        )
        db.flush()

    assert user.entitlement_grace_ends_at == first


def test_p7_a_frozen_account_does_not_accrue_a_bucket_a_month():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    with SyncSessionLocal() as db:
        user = _mk_user(
            db, records_used=1000, quota_period_start=start,
            quota_period_end=datetime(2026, 7, 1, tzinfo=UTC),
            subscription_status="past_due",
            entitlement_grace_ends_at=datetime(2026, 6, 8, tzinfo=UTC),
        )
        db.flush()

    # Three months later, still unpaid: no rollover, no fresh quota.
    assert should_roll(user, datetime(2026, 9, 1, tzinfo=UTC)) is False
    assert effective_records_used(user, datetime(2026, 9, 1, tzinfo=UTC)) == 1000


def test_p7_recovery_grants_exactly_one_window_not_one_per_frozen_month():
    start = datetime(2026, 6, 1, tzinfo=UTC)
    with SyncSessionLocal() as db:
        user = _mk_user(
            db, records_used=1000, quota_period_start=start,
            quota_period_end=datetime(2026, 7, 1, tzinfo=UTC),
            subscription_status="past_due",
            entitlement_grace_ends_at=datetime(2026, 6, 8, tzinfo=UTC),
        )
        recovered_at = datetime(2026, 9, 6, tzinfo=UTC)
        mark_payment_succeeded(user, status="active", now=recovered_at)
        db.flush()

    assert user.entitlement_grace_ends_at is None
    assert is_frozen(user, recovered_at) is False
    start_w, end_w = effective_window(user, recovered_at)
    assert start_w == datetime(2026, 9, 1, tzinfo=UTC)
    assert end_w == datetime(2026, 10, 1, tzinfo=UTC)


# ─── P8: unpaid and the other non-entitled statuses ───────────────────────────

def test_p8_unpaid_freezes_immediately_and_deletes_nothing():
    with SyncSessionLocal() as db:
        user = _mk_user(db, plan="pro", records_used=400, records_limit=1000,
                        subscription_status="unpaid", first_paid_at=NOW)
        db.flush()

    assert is_frozen(user, NOW) is True
    assert user.plan == "pro", "plan and limit are untouched"
    assert user.records_limit == 1000
    assert user.records_used == 400, "no customer data or usage is destroyed"


def test_p8_starter_and_free_accounts_are_never_frozen():
    with SyncSessionLocal() as db:
        user = _mk_user(db, plan="starter", records_limit=50,
                        subscription_status=None)
        db.flush()

    assert is_frozen(user, NOW) is False
    assert should_roll(user, user.quota_period_end + timedelta(seconds=1)) is True


# ─── P9: resubscribe ──────────────────────────────────────────────────────────

def test_p9_resubscribing_inside_a_live_entitlement_mints_nothing():
    """Cancel-then-resubscribe before the term ends fires no `deleted` event, so
    paid_entitlement_ended_at is never set and there is nothing to farm."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, records_used=800, subscription_status="active",
                        first_paid_at=NOW - timedelta(days=40),
                        entitlement_ends_at=NOW + timedelta(days=10))
        window = (user.quota_anchor_at, user.quota_period_start, user.quota_period_end)

        reset = activate_paid_plan(
            user, plan="pro", records_limit=1000, subscription_id="sub_2",
            status="active", billing_cycle_anchor=NOW, now=NOW,
        )
        db.flush()

    assert reset is False
    assert user.records_used == 800
    assert (user.quota_anchor_at, user.quota_period_start, user.quota_period_end) == window


def test_p9_resubscribing_after_a_genuine_lapse_starts_a_new_entitlement():
    with SyncSessionLocal() as db:
        user = _mk_user(db, plan="starter", records_used=800, records_limit=50,
                        first_paid_at=NOW - timedelta(days=90),
                        paid_entitlement_ended_at=NOW - timedelta(days=30),
                        subscription_status="canceled")
        reset = activate_paid_plan(
            user, plan="pro", records_limit=1000, subscription_id="sub_3",
            status="active", billing_cycle_anchor=NOW, now=NOW,
        )
        db.flush()

    assert reset is True
    assert user.records_used == 0
    assert user.quota_period_start == NOW
    assert user.paid_entitlement_ended_at is None, (
        "cleared in the same call, so a replay cannot re-mint"
    )


def test_p9_a_replayed_resubscribe_cannot_mint_a_second_window():
    with SyncSessionLocal() as db:
        user = _mk_user(db, plan="starter", records_limit=50,
                        first_paid_at=NOW - timedelta(days=90),
                        paid_entitlement_ended_at=NOW - timedelta(days=30))
        activate_paid_plan(
            user, plan="pro", records_limit=1000, subscription_id="sub_3",
            status="active", billing_cycle_anchor=NOW, now=NOW,
        )
        user.records_used = 450
        reset_again = activate_paid_plan(
            user, plan="pro", records_limit=1000, subscription_id="sub_3",
            status="active", billing_cycle_anchor=NOW,
            now=NOW + timedelta(minutes=5),
        )
        db.flush()

    assert reset_again is False
    assert user.records_used == 450


def test_p9_the_trial_is_never_granted_twice():
    """trial_consumed_at is the anti-farming control, not the window logic:
    trial_ends_at is CLEARED on conversion and so cannot answer the question."""
    with SyncSessionLocal() as db:
        user = _mk_user(db, trial_ends_at=NOW + timedelta(days=3))
        activate_paid_plan(
            user, plan="pro", records_limit=1000, subscription_id="sub_1",
            status="active", billing_cycle_anchor=NOW, now=NOW,
        )
        db.flush()
        consumed = user.trial_consumed_at

        end_subscription(user, now=NOW + timedelta(days=40))
        db.flush()

    assert user.trial_ends_at is None
    assert user.trial_consumed_at == consumed, "still spent after cancelling"


# ─── An entitled state reached through `updated` alone ────────────────────────

def test_a_conversion_that_arrives_as_subscription_updated_still_grants_a_month():
    """Checkout usually gets there first, but a Stripe-side trial converting (or
    an incomplete payment finally succeeding) can reach an entitled state through
    `updated` alone. That path must not be the one that forgets the paid month.
    """
    with SyncSessionLocal() as db:
        user = _mk_user(db, records_used=1000, trial_ends_at=NOW)
        outcome = apply_plan_change(
            user, plan="pro", records_limit=1000, subscription_id="sub_1",
            status="active", cancel_at_period_end=False, entitlement_end=None,
            billing_cycle_anchor=NOW, now=NOW,
        )
        db.flush()

    assert outcome == "converted"
    assert user.records_used == 0
    assert user.first_paid_at == NOW


# ─── The migration ────────────────────────────────────────────────────────────

def _migration_088():
    """Load the real migration module so the tests run its ACTUAL backfill SQL.

    Importing rather than transcribing matters here: a migration test that
    re-types the statement only proves the transcription is self-consistent.
    """
    path = (
        settings.BASE_DIR
        / "alembic" / "versions" / "088_quota_entitlement_periods.py"
    )
    spec = importlib.util.spec_from_file_location("_mig088", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_backfill(db) -> None:
    mig = _migration_088()
    for stmt in (
        mig.BACKFILL_WINDOWS, mig.BACKFILL_FIRST_PAID, mig.BACKFILL_TRIAL_CONSUMED
    ):
        db.execute(text(stmt))


def test_migration_backfill_changes_no_existing_users_quota():
    """The deploy must not move anyone's reset date, grant a bucket or take one.

    Every existing records_period_start is the first of a month, so every user
    lands on a day-1 grid — precisely the calendar behaviour they already had.
    """
    period = datetime(2026, 9, 1, tzinfo=UTC)
    with SyncSessionLocal() as db:
        user = _mk_user(db, records_used=742, records_limit=1000,
                        quota_period_start=period)
        user.records_period_start = period
        user_id = user.id
        db.commit()

        _run_backfill(db)
        db.commit()

        fresh = db.get(User, user_id)
        db.refresh(fresh)
        assert fresh.records_used == 742
        assert fresh.quota_anchor_at == period
        assert fresh.quota_period_start == period
        assert fresh.quota_period_end == datetime(2026, 10, 1, tzinfo=UTC)


def test_migration_backfill_is_idempotent():
    """Recomputed from records_period_start, never accumulated — so a re-run, or
    a repeat after a partial failure, is a no-op."""
    period = datetime(2026, 9, 1, tzinfo=UTC)
    with SyncSessionLocal() as db:
        user = _mk_user(db, records_used=500, quota_period_start=period)
        user.records_period_start = period
        user_id = user.id
        db.commit()

        _run_backfill(db)
        db.commit()
        first = (
            db.get(User, user_id).quota_period_start,
            db.get(User, user_id).quota_period_end,
        )
        _run_backfill(db)
        _run_backfill(db)
        db.commit()
        fresh = db.get(User, user_id)
        db.refresh(fresh)

        assert (fresh.quota_period_start, fresh.quota_period_end) == first
        assert fresh.records_used == 500


def test_migration_does_not_rescue_the_known_over_cap_account():
    """The live account read 1,007/1,000 at handoff and that number is CORRECT —
    1,001 restored by the incident repair plus 6 consumed by a reservation canary.

    Adding columns must not quietly make an over-cap account look tidy. It stays
    over cap, blocked, until its own boundary.
    """
    period = datetime(2026, 9, 1, tzinfo=UTC)
    with SyncSessionLocal() as db:
        user = _mk_user(db, records_used=1007, records_limit=1000,
                        quota_period_start=period)
        user.records_period_start = period
        user_id = user.id
        db.commit()

        _run_backfill(db)
        db.commit()
        fresh = db.get(User, user_id)
        db.refresh(fresh)

        assert fresh.records_used == 1007
        assert is_over_record_limit(fresh, datetime(2026, 9, 6, tzinfo=UTC)) is True
        assert fresh.quota_period_end == datetime(2026, 10, 1, tzinfo=UTC)


def test_migration_marks_existing_payers_as_already_converted():
    """Otherwise their next subscription webhook looks like a FIRST conversion
    and zeroes a counter they legitimately owe."""
    with SyncSessionLocal() as db:
        payer = _mk_user(db, subscription_status="active", records_used=300)
        payer.records_period_start = datetime(2026, 9, 1, tzinfo=UTC)
        free = _mk_user(db, subscription_status=None)
        free.records_period_start = datetime(2026, 9, 1, tzinfo=UTC)
        payer_id, free_id = payer.id, free.id
        db.commit()

        _run_backfill(db)
        db.commit()
        # The backfill is raw SQL, so the identity map still holds the pre-UPDATE
        # objects. Expire them or the assertions read stale attributes.
        db.expire_all()

        assert db.get(User, payer_id).first_paid_at is not None
        assert db.get(User, free_id).first_paid_at is None


def test_the_legacy_calendar_reset_no_longer_governs_record_quota():
    """The completion bar: running the surviving daily task must not touch the
    record counter of a user whose window is anchored away from the 1st."""
    from src.workers.scheduler import reset_skip_trace_usage

    start = datetime(2026, 8, 20, tzinfo=UTC)
    with SyncSessionLocal() as db:
        user = _mk_user(
            db, records_used=900, quota_period_start=start,
            quota_period_end=datetime(2026, 9, 20, tzinfo=UTC),
            subscription_status="active",
        )
        # A records_period_start in a PREVIOUS month is exactly what the retired
        # task keyed on. It must now be inert for record quota.
        user.records_period_start = start
        user_id = user.id
        db.commit()

    reset_skip_trace_usage()

    with SyncSessionLocal() as db:
        fresh = db.get(User, user_id)
        assert fresh.records_used == 900
        assert fresh.quota_period_end == datetime(2026, 9, 20, tzinfo=UTC)

# ─── Defects found by the Codex review of this diff ──────────────────────────
#
# Each of these FAILED against the first implementation. They are kept as the
# regression bar: every one is a way a customer ends up with the wrong number.

def test_an_agency_downgrade_does_not_skip_the_cap_after_the_boundary():
    """P1 (Codex): the cap block is skipped entirely for unlimited users.

    An Agency subscriber (records_limit -1) with a pending downgrade to Pro whose
    window has ENDED would export uncapped, and settlement would only then roll
    the window and apply the Pro limit — landing them at 5000/1000. The cap
    decision has to be made against the limit that will actually apply.
    """
    from src.api.quota import effective_records_limit

    ended = NOW - timedelta(minutes=1)
    with SyncSessionLocal() as db:
        user = _mk_user(
            db, plan="agency", records_limit=-1, records_used=0,
            quota_period_start=ended - timedelta(days=30),
            quota_period_end=ended,
            pending_plan="pro", pending_records_limit=1000,
            subscription_status="active",
        )
        db.flush()

    assert should_roll(user, NOW) is True
    assert effective_records_limit(user, NOW) == 1000, (
        "the cap block must run, and reserve against the incoming Pro limit"
    )
    # Inside a LIVE window the paid-for unlimited plan still applies.
    with SyncSessionLocal() as db:
        live = _mk_user(
            db, plan="agency", records_limit=-1,
            pending_plan="pro", pending_records_limit=1000,
            subscription_status="active",
        )
        db.flush()
    assert effective_records_limit(live, NOW) == -1
    assert is_over_record_limit(live, NOW) is False


def test_a_stripe_side_trial_is_not_backfilled_as_already_paid():
    """P1 (Codex): a `trialing` subscription has not paid anything.

    Stamping first_paid_at for them would make their eventual conversion look
    like an ordinary plan change, so a customer who consumed 1,000/1,000 on trial
    and then paid $199 would stay at 1,000/1,000 — the exact defect this whole
    change exists to fix.
    """
    with SyncSessionLocal() as db:
        trialing = _mk_user(db, subscription_status="trialing", records_used=1000)
        trialing.records_period_start = datetime(2026, 9, 1, tzinfo=UTC)
        paying = _mk_user(db, subscription_status="active")
        paying.records_period_start = datetime(2026, 9, 1, tzinfo=UTC)
        trialing_id, paying_id = trialing.id, paying.id
        db.commit()

        _run_backfill(db)
        db.commit()
        db.expire_all()

        assert db.get(User, trialing_id).first_paid_at is None
        assert db.get(User, paying_id).first_paid_at is not None

    # ...and their conversion therefore still grants the paid month.
    with SyncSessionLocal() as db:
        user = db.get(User, trialing_id)
        outcome = apply_plan_change(
            user, plan="pro", records_limit=1000, subscription_id="sub_1",
            status="active", cancel_at_period_end=False, entitlement_end=None,
            billing_cycle_anchor=NOW, now=NOW,
        )
        db.commit()

    assert outcome == "converted"
    assert user.records_used == 0


def test_reconciliation_asks_stripe_before_taking_a_plan_away():
    """P2 (Codex): the cancellation-REVERSAL webhook can go missing too.

    A customer who scheduled a cancel and then reversed it would otherwise be
    downgraded on the stale end date — and could later "resubscribe" into another
    fresh window. Same doctrine as expire_trials: Stripe is the truth, and an
    error means UNKNOWN, never a downgrade.
    """
    from src.workers.scheduler_helpers.billing import _reconcile_quota_periods_impl

    past = datetime(2020, 2, 1, tzinfo=UTC)
    # stripe_customer_id is UNIQUE, and these rows outlive the test.
    live_cus = f"cus_live_{uuid.uuid4().hex[:8]}"
    gone_cus = f"cus_gone_{uuid.uuid4().hex[:8]}"
    with SyncSessionLocal() as db:
        reversed_ = _mk_user(
            db, plan="pro", records_limit=1000,
            quota_period_start=datetime(2020, 1, 1, tzinfo=UTC),
            quota_period_end=past, entitlement_ends_at=past,
            subscription_status="active", stripe_customer_id=live_cus,
            stripe_subscription_id="sub_live",
        )
        genuine = _mk_user(
            db, plan="pro", records_limit=1000,
            quota_period_start=datetime(2020, 1, 1, tzinfo=UTC),
            quota_period_end=past, entitlement_ends_at=past,
            subscription_status="active", stripe_customer_id=gone_cus,
            stripe_subscription_id="sub_gone",
        )
        reversed_id, genuine_id = reversed_.id, genuine.id
        db.commit()

    _reconcile_quota_periods_impl(
        subscription_lookup=lambda cid: "active" if cid == live_cus else None
    )

    with SyncSessionLocal() as db:
        still_paying = db.get(User, reversed_id)
        cancelled = db.get(User, genuine_id)

    assert still_paying.plan == "pro", "an active payer must not be downgraded"
    assert still_paying.entitlement_ends_at is None, "the stale end date is cleared"
    assert still_paying.paid_entitlement_ended_at is None, (
        "no lapse was recorded, so they cannot later resubscribe into a reset"
    )
    assert cancelled.plan == "starter"
    assert cancelled.paid_entitlement_ended_at is not None


def test_reconciliation_never_downgrades_on_a_stripe_error():
    """A transient Stripe failure must not cost a customer their plan."""
    from src.workers.scheduler_helpers.billing import _reconcile_quota_periods_impl

    past = datetime(2020, 2, 1, tzinfo=UTC)
    cus = f"cus_err_{uuid.uuid4().hex[:8]}"

    def _boom(_customer_id):
        raise RuntimeError("stripe timeout")

    with SyncSessionLocal() as db:
        user = _mk_user(
            db, plan="business", records_limit=5000,
            quota_period_start=datetime(2020, 1, 1, tzinfo=UTC),
            quota_period_end=past, entitlement_ends_at=past,
            subscription_status="active", stripe_customer_id=cus,
        )
        user_id = user.id
        db.commit()

    _reconcile_quota_periods_impl(subscription_lookup=_boom)

    with SyncSessionLocal() as db:
        fresh = db.get(User, user_id)

    assert fresh.plan == "business"
    assert fresh.entitlement_ends_at == past, "left for the next run to retry"


async def test_a_new_signup_cannot_be_zeroed_by_the_retired_calendar_reset(db):
    """P2 (Codex): mixed deploy.

    The API can be new while a worker is still running the retired calendar
    reset, which zeroes rows whose records_period_start is in an earlier month.
    A signup-dated mirror column would let it wipe a brand-new trial user's
    counter and hand them a second 1,000 records inside one 7-day trial. The
    mirror therefore stays on the month start at signup — harmless, because the
    ledger it scopes is empty until the first charge rewrites it in lockstep.
    """
    from src.api.routes.auth_helpers.registration import _create_real_user

    user = await _create_real_user(
        db,
        email=f"mix_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        first_name="Mixed", last_name="Deploy",
        password_hash=hash_password("TestPass123!"),
        referred_by_id=None, referral_code=uuid.uuid4().hex[:8].upper(),
    )
    await db.flush()

    month_start = datetime.now(UTC).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    assert as_utc(user.records_period_start) == month_start, (
        "the mirror must not match the retired task's stale predicate"
    )
    # ...while the real entitlement window is the 7-day trial.
    assert as_utc(user.quota_period_start) > month_start
    assert (
        as_utc(user.quota_period_end) - as_utc(user.quota_period_start)
    ) < timedelta(days=8)


# ─── Round-2 Codex findings: legacy payers ────────────────────────────────────
#
# A "legacy payer" is a real paying customer from before migration 077 recorded a
# durable subscription_status. Both of these hand such a customer a free window,
# and both were live in the first cut.

def test_a_known_subscription_is_not_treated_as_a_first_conversion():
    """P1 (Codex): a legacy payer with first_paid_at NULL.

    Their next routine customer.subscription.updated would otherwise take the
    conversion branch and zero the counter of someone who has been paying for
    months. Having already recorded THIS subscription id is the tell: on a
    genuine first conversion checkout binds the id and stamps first_paid_at in
    the same locked transaction, so the two can never be out of step that way.
    """
    with SyncSessionLocal() as db:
        legacy = _mk_user(
            db, plan="pro", records_used=640, records_limit=1000,
            subscription_status=None, first_paid_at=None,
            stripe_subscription_id="sub_legacy",
        )
        outcome = apply_plan_change(
            legacy, plan="pro", records_limit=1000, subscription_id="sub_legacy",
            status="active", cancel_at_period_end=False, entitlement_end=None,
            billing_cycle_anchor=NOW, now=NOW,
        )
        db.flush()

    assert outcome != "converted"
    assert legacy.records_used == 640, "a paying customer keeps their usage"


def test_an_unknown_subscription_still_converts():
    """The guard must not block a REAL conversion that arrives as an update
    before checkout has bound the subscription id."""
    with SyncSessionLocal() as db:
        user = _mk_user(
            db, records_used=1000, trial_ends_at=NOW,
            subscription_status=None, first_paid_at=None,
            stripe_subscription_id=None,
        )
        outcome = apply_plan_change(
            user, plan="pro", records_limit=1000, subscription_id="sub_new",
            status="active", cancel_at_period_end=False, entitlement_end=None,
            billing_cycle_anchor=NOW, now=NOW,
        )
        db.flush()

    assert outcome == "converted"
    assert user.records_used == 0


def test_the_migration_marks_unambiguous_legacy_payers_as_converted():
    """P1 (Codex): status NULL, on a paid tier, reached Stripe, trial cleared.

    The checkout handler is what clears trial_ends_at, so a NULL trial on a paid
    tier with a Stripe customer is a converted payer whose status column simply
    predates migration 077. Someone still carrying a trial_ends_at is NOT
    reachable from here — indistinguishable from an abandoned checkout — and is
    left to expire_trials, which asks Stripe.
    """
    period = datetime(2026, 9, 1, tzinfo=UTC)
    with SyncSessionLocal() as db:
        legacy = _mk_user(
            db, plan="pro", subscription_status=None, trial_ends_at=None,
            stripe_customer_id=f"cus_legacy_{uuid.uuid4().hex[:8]}",
            quota_period_start=period,
        )
        legacy.records_period_start = period
        ambiguous = _mk_user(
            db, plan="pro", subscription_status=None,
            trial_ends_at=NOW + timedelta(days=2),
            stripe_customer_id=f"cus_maybe_{uuid.uuid4().hex[:8]}",
            quota_period_start=period,
        )
        ambiguous.records_period_start = period
        never = _mk_user(
            db, plan="pro", subscription_status=None, trial_ends_at=None,
            stripe_customer_id=None, quota_period_start=period,
        )
        never.records_period_start = period
        legacy_id, ambiguous_id, never_id = legacy.id, ambiguous.id, never.id
        db.commit()

        _run_backfill(db)
        db.commit()
        db.expire_all()

        assert db.get(User, legacy_id).first_paid_at is not None
        assert db.get(User, ambiguous_id).first_paid_at is None, (
            "left for expire_trials to resolve against Stripe"
        )
        assert db.get(User, never_id).first_paid_at is None
