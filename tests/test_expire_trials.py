"""expire_trials gates on durable Stripe ENTITLEMENT, not stripe_customer_id.

The old gate (`stripe_customer_id IS NULL`) let a trial user who merely OPENED
checkout — which creates a customer id but no payment — keep Pro forever. The fix
(migration 077) keys on stripe_subscription_id + subscription_status, and for an
AMBIGUOUS row (customer id present but status NULL — a legacy payer OR an abandoned
checkout) it asks Stripe (the source of truth). The Stripe lookup is dependency-
injected here so tests never hit the network (no mocks).

DB-backed (real Postgres, sync — like test_batch_dispatch). No mocks.
"""
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from src.api.auth import hash_password
from src.db.models import User
from src.db.session import SyncSessionLocal
from src.workers.scheduler_helpers.billing import _expire_trials_impl


# Injected Stripe lookups (replace the live stripe.Subscription.list call).
def _lookup_none(customer_id):
    """No entitled subscription."""
    return None


def _lookup_active(customer_id):
    """Entitled subscriber."""
    return "active"


def _boom(customer_id):
    raise RuntimeError("stripe down")


def _make_user(
    db,
    *,
    trial_offset_days: float | None,
    plan: str = "pro",
    stripe_customer_id: str | None = None,
    stripe_subscription_id: str | None = None,
    subscription_status: str | None = None,
) -> str:
    """Create a user; trial_offset_days<0 = expired, >0 = active, None = no trial."""
    trial_ends_at = (
        None if trial_offset_days is None
        else datetime.now(UTC) + timedelta(days=trial_offset_days)
    )
    user = User(
        id=str(uuid.uuid4()),
        email=f"expire_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"),
        plan=plan,
        records_used=0,
        records_limit=5000,
        trial_ends_at=trial_ends_at,
        stripe_customer_id=stripe_customer_id,
        stripe_subscription_id=stripe_subscription_id,
        subscription_status=subscription_status,
    )
    db.add(user)
    db.flush()
    return user.id


def _get(db, user_id: str) -> User:
    return db.get(User, user_id)


@pytest.fixture
def cleanup_users():
    ids: list[str] = []
    yield ids
    with SyncSessionLocal() as db:
        for uid in ids:
            obj = db.get(User, uid)
            if obj is not None:
                db.delete(obj)
        db.commit()


def _create(cleanup_users, **kw) -> str:
    with SyncSessionLocal() as db:
        uid = _make_user(db, **kw)
        cleanup_users.append(uid)
        db.commit()
    return uid


def test_expired_trial_no_stripe_is_downgraded(cleanup_users):
    uid = _create(cleanup_users, trial_offset_days=-1)
    _expire_trials_impl(subscription_lookup=_lookup_none)
    with SyncSessionLocal() as db:
        assert _get(db, uid).plan == "starter"


def test_ambiguous_row_with_entitled_stripe_sub_is_protected(cleanup_users):
    """Legacy payer: customer id present, status NULL, but Stripe says active.
    Must be protected AND self-healed (status backfilled so we skip Stripe next run)."""
    uid = _create(
        cleanup_users, trial_offset_days=-1,
        stripe_customer_id="cus_legacy_payer", subscription_status=None,
    )
    _expire_trials_impl(subscription_lookup=_lookup_active)
    with SyncSessionLocal() as db:
        u = _get(db, uid)
        assert u.plan == "pro", "legacy payer must NOT be downgraded"
        assert u.subscription_status == "active", "status should be self-healed"


def test_ambiguous_row_abandoned_checkout_is_downgraded(cleanup_users):
    """Abandoned checkout: customer id present, status NULL, Stripe shows no
    entitled sub -> downgrade (this is the original bug, now fixed automatically)."""
    uid = _create(
        cleanup_users, trial_offset_days=-1,
        stripe_customer_id="cus_abandoned", subscription_status=None,
    )
    _expire_trials_impl(subscription_lookup=_lookup_none)
    with SyncSessionLocal() as db:
        u = _get(db, uid)
        assert u.plan == "starter"
        assert u.subscription_status == "canceled", "non-entitlement recorded"


def test_ambiguous_row_is_not_downgraded_on_stripe_error(cleanup_users):
    """Transient Stripe failure -> never downgrade a possible payer."""
    uid = _create(
        cleanup_users, trial_offset_days=-1,
        stripe_customer_id="cus_x", subscription_status=None,
    )
    _expire_trials_impl(subscription_lookup=_boom)
    with SyncSessionLocal() as db:
        assert _get(db, uid).plan == "pro"


@pytest.mark.parametrize("status", ["active", "trialing", "past_due"])
def test_entitled_status_is_protected(cleanup_users, status):
    uid = _create(
        cleanup_users, trial_offset_days=-1,
        stripe_customer_id="cus_paying", stripe_subscription_id="sub_1",
        subscription_status=status,
    )
    _expire_trials_impl(subscription_lookup=_boom)  # must not even call Stripe
    with SyncSessionLocal() as db:
        assert _get(db, uid).plan == "pro", f"{status} must protect from expiry"


def test_canceled_status_is_downgraded_once_stripe_confirms_it(cleanup_users):
    """A locally-recorded "canceled" is now VERIFIED before it costs a plan.

    This test previously asserted the opposite — that a stored non-entitled
    status was acted on without asking Stripe. That is wrong in the one
    direction that matters: our copy of the status is a cache, it can be stale
    (a mis-ordered webhook, a cancellation the customer reversed), and
    downgrading on it takes a plan away from someone who is paying. Stripe is
    the source of truth for entitlement. (Codex)
    """
    uid = _create(
        cleanup_users, trial_offset_days=-1,
        stripe_customer_id="cus_x", stripe_subscription_id="sub_dead",
        subscription_status="canceled",
    )
    _expire_trials_impl(subscription_lookup=lambda _c: None)  # Stripe agrees
    with SyncSessionLocal() as db:
        assert _get(db, uid).plan == "starter"


def test_a_canceled_status_stripe_contradicts_does_not_cost_the_plan(cleanup_users):
    """The reason the verification exists: our cache said canceled, Stripe says
    active. The customer is paying, so they keep their plan — and the stale
    status is healed rather than acted on."""
    uid = _create(
        cleanup_users, trial_offset_days=-1,
        stripe_customer_id="cus_really_paying", stripe_subscription_id="sub_live",
        subscription_status="canceled",
    )
    _expire_trials_impl(subscription_lookup=lambda _c: "active")
    with SyncSessionLocal() as db:
        user = _get(db, uid)
        assert user.plan == "pro", "an active payer must not be downgraded"
        assert user.subscription_status == "active", "the stale cache is healed"
        assert user.first_paid_at is not None, (
            "and they are marked as already converted, so their next "
            "subscription.updated cannot read as a first conversion and zero "
            "their counter"
        )


def test_a_stripe_outage_defers_the_downgrade_rather_than_guessing(cleanup_users):
    """Never downgrade a possible payer on a transient failure — the standing
    doctrine, now applied to stored-status rows too. The row is simply retried
    on the next hourly run."""
    uid = _create(
        cleanup_users, trial_offset_days=-1,
        stripe_customer_id="cus_unknown", stripe_subscription_id="sub_?",
        subscription_status="canceled",
    )
    _expire_trials_impl(subscription_lookup=_boom)
    with SyncSessionLocal() as db:
        assert _get(db, uid).plan == "pro"


def test_active_trial_is_not_downgraded(cleanup_users):
    uid = _create(cleanup_users, trial_offset_days=3)  # trial still running
    _expire_trials_impl(subscription_lookup=_boom)
    with SyncSessionLocal() as db:
        assert _get(db, uid).plan == "pro"


def test_user_without_trial_is_untouched(cleanup_users):
    """e.g. admin/agency with trial_ends_at NULL — gate requires a trial date."""
    uid = _create(cleanup_users, trial_offset_days=None, plan="agency")
    _expire_trials_impl(subscription_lookup=_boom)
    with SyncSessionLocal() as db:
        assert _get(db, uid).plan == "agency"
