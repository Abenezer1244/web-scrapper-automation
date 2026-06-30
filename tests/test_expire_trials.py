"""expire_trials gates on durable Stripe ENTITLEMENT, not stripe_customer_id.

The old gate (`stripe_customer_id IS NULL`) let a trial user who merely OPENED
checkout — which creates a customer id but no payment — keep Pro forever after
their trial. The fix (migration 077) keys on stripe_subscription_id +
subscription_status: only an entitled status (active/trialing/past_due) protects
a user from trial expiry.

DB-backed (real Postgres, sync — like test_batch_dispatch). No mocks.
"""
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from src.api.auth import hash_password
from src.db.models import User
from src.db.session import SyncSessionLocal
from src.workers.scheduler_helpers.billing import _expire_trials_impl


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


def _plan_of(db, user_id: str) -> str:
    return db.get(User, user_id).plan


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


def test_expired_trial_no_stripe_is_downgraded(cleanup_users):
    with SyncSessionLocal() as db:
        uid = _make_user(db, trial_offset_days=-1)
        cleanup_users.append(uid)
        db.commit()

    _expire_trials_impl()

    with SyncSessionLocal() as db:
        assert _plan_of(db, uid) == "starter"


def test_expired_trial_with_customer_but_no_subscription_is_downgraded(cleanup_users):
    """THE BUG: opened checkout (has customer id) but never paid -> must downgrade."""
    with SyncSessionLocal() as db:
        uid = _make_user(
            db, trial_offset_days=-1,
            stripe_customer_id="cus_opened_checkout_no_pay",
            stripe_subscription_id=None,
            subscription_status=None,
        )
        cleanup_users.append(uid)
        db.commit()

    _expire_trials_impl()

    with SyncSessionLocal() as db:
        assert _plan_of(db, uid) == "starter"


@pytest.mark.parametrize("status", ["active", "trialing", "past_due"])
def test_expired_trial_with_entitled_subscription_is_protected(cleanup_users, status):
    with SyncSessionLocal() as db:
        uid = _make_user(
            db, trial_offset_days=-1,
            stripe_customer_id="cus_paying",
            stripe_subscription_id="sub_123",
            subscription_status=status,
        )
        cleanup_users.append(uid)
        db.commit()

    _expire_trials_impl()

    with SyncSessionLocal() as db:
        assert _plan_of(db, uid) == "pro", f"{status} must protect from trial expiry"


def test_expired_trial_with_canceled_subscription_is_downgraded(cleanup_users):
    with SyncSessionLocal() as db:
        uid = _make_user(
            db, trial_offset_days=-1,
            stripe_customer_id="cus_x",
            stripe_subscription_id="sub_dead",
            subscription_status="canceled",
        )
        cleanup_users.append(uid)
        db.commit()

    _expire_trials_impl()

    with SyncSessionLocal() as db:
        assert _plan_of(db, uid) == "starter"


def test_active_trial_is_not_downgraded(cleanup_users):
    with SyncSessionLocal() as db:
        uid = _make_user(db, trial_offset_days=3)  # trial still running
        cleanup_users.append(uid)
        db.commit()

    _expire_trials_impl()

    with SyncSessionLocal() as db:
        assert _plan_of(db, uid) == "pro"


def test_user_without_trial_is_untouched(cleanup_users):
    """e.g. admin/agency with trial_ends_at NULL — gate requires a trial date."""
    with SyncSessionLocal() as db:
        uid = _make_user(db, trial_offset_days=None, plan="agency")
        cleanup_users.append(uid)
        db.commit()

    _expire_trials_impl()

    with SyncSessionLocal() as db:
        assert _plan_of(db, uid) == "agency"
