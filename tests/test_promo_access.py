"""A fully discounted paid plan is still the paid plan.

A single-customer Stripe promotion (100% off Agency for 3 months) changes the
PRICE of a subscription, never its product. These tests pin what that means for
BridgeLeads:

  * a $0 checkout (payment_status no_payment_required, no PaymentIntent)
    activates exactly the entitlement a paying Agency customer gets;
  * replays and the discount ending never reset quota or move the window;
  * a time-limited coupon cannot reach an annual invoice, where Stripe would
    discount the whole year;
  * the webhook plumbing a $0 subscription depends on survives a handler
    failure, an in-flight duplicate, an `incomplete` subscription and a stray
    deletion.

Stripe is the one dependency stood in for, at the network boundary, as the
existing billing tests do: real user rows, real Postgres, real Redis, the real
routes and the real webhook signature check.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import create_secure_token, hash_password
from src.config.constants import (
    ALL_RECORD_TYPES,
    COUNTY_LIMIT_BY_PLAN,
    RECORD_TYPES_BY_PLAN,
    normalize_plan,
)
from src.config.settings import settings
from src.db.models import User

_WEBHOOK_SECRET = "whsec_promo_access_signature_secret_0123456789"

_COUPON_3_MONTHS = {
    "id": "cpn_agency_3m",
    "percent_off": 100,
    "duration": "repeating",
    "duration_in_months": 3,
}


def _billing():
    from src.api.routes import billing as b
    return b


def _price(plan: str, interval: str) -> str:
    b = _billing()
    pid = next(
        (p for p, info in b._PRICE_TO_PLAN.items() if info[0] == plan and info[2] == interval),
        None,
    )
    if pid is None:
        pytest.skip(f"no {plan}/{interval} STRIPE_PRICE_* configured in this environment")
    return pid


def _agency_subscription(
    *, status: str = "active", sub_id: str = "sub_promo", discounts=None, interval="month"
) -> dict:
    b = _billing()
    items = [{"id": "si_plan", "price": {"id": _price("agency", interval)}}]
    metered = b._metered_skip_trace_price("agency", interval)
    if metered:
        items.append({"id": "si_metered", "price": {"id": metered}})
    return {
        "id": sub_id,
        "customer": "cus_promo",
        "status": status,
        "items": {"data": items},
        "billing_cycle_anchor": int(time.time()),
        "cancel_at_period_end": False,
        "current_period_end": int(time.time()) + 30 * 86400,
        "discounts": [
            {"id": "di_promo", "coupon": _COUPON_3_MONTHS,
             "end": int(time.time()) + 90 * 86400}
        ] if discounts is None else discounts,
    }


def _zero_dollar_session(user_id: str, sub_id: str = "sub_promo") -> dict:
    """The checkout.session.completed object for a fully discounted session."""
    return {
        "id": "cs_test_promo",
        "object": "checkout.session",
        "mode": "subscription",
        "customer": "cus_promo",
        "subscription": sub_id,
        "metadata": {"user_id": user_id},
        "payment_status": "no_payment_required",
        "payment_intent": None,
        "amount_total": 0,
        "total_details": {"amount_discount": 149900},
    }


class _List:
    """A Stripe ListObject that only pages, like the one production iterates."""

    def __init__(self, items):
        self._items = list(items)

    def auto_paging_iter(self):
        return iter(self._items)


async def _trial_user(db: AsyncSession, **overrides) -> User:
    now = datetime.now(UTC)
    fields = {
        "plan": "pro",
        "records_used": 900,
        "records_limit": 1000,
        "trial_ends_at": now + timedelta(days=3),
        "quota_anchor_at": now - timedelta(days=4),
        "quota_period_start": now - timedelta(days=4),
        "quota_period_end": now + timedelta(days=3),
        "records_period_start": now - timedelta(days=4),
        "stripe_customer_id": "cus_promo",
    }
    fields.update(overrides)
    user = User(
        id=str(uuid.uuid4()),
        email=f"promo_{uuid.uuid4().hex[:10]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"),
        **fields,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _reload(db: AsyncSession, user_id: str) -> User:
    db.expire_all()
    return (await db.execute(select(User).where(User.id == user_id))).scalar_one()


# ─── Coupon shape rules ───────────────────────────────────────────────────────

def test_only_a_coupon_tagged_by_the_promotion_script_is_a_single_customer_promo():
    """FOUNDING25 and any other general coupon must never trip the annual alert."""
    b = _billing()
    tagged = {**_COUPON_3_MONTHS, "metadata": {"bridgeleads_resource": "single_customer_promo"}}
    assert b._is_single_customer_promo(tagged) is True
    assert b._is_single_customer_promo(_COUPON_3_MONTHS) is False
    assert b._is_single_customer_promo(
        {"id": "FOUNDING25", "percent_off": 25, "duration": "forever", "metadata": {}}
    ) is False
    assert b._is_single_customer_promo(None) is False


def test_the_discount_coupon_is_found_on_every_api_shape(monkeypatch):
    """Older API versions embed the coupon; newer ones nest it, possibly by id."""
    b = _billing()
    full = {**_COUPON_3_MONTHS, "metadata": {"bridgeleads_resource": "single_customer_promo"}}
    assert b._coupon_of({"coupon": full}) == full
    monkeypatch.setattr(b.stripe.Coupon, "retrieve", lambda cid, **kw: full)
    assert b._coupon_of({"source": {"coupon": "cpn_agency_3m"}}) == full
    # A partial coupon (no metadata) is fetched whole, or the alert could miss it.
    assert b._coupon_of({"coupon": {"id": "cpn_agency_3m"}}) == full


def test_a_price_listed_twice_as_legacy_is_not_trusted_either_way():
    b = _billing()
    parsed = b._legacy_plan_prices(
        "price_dup:agency:year, price_dup:pro:month, price_ok:business:month", {}
    )
    assert parsed == {"price_ok": ("business", 5000, "month")}


def test_legacy_plan_prices_are_parsed_strictly():
    b = _billing()
    sold = {"price_sold_now": ("agency", -1, "month")}
    parsed = b._legacy_plan_prices(
        "price_old_agency:agency:month, price_old_pro:pro:year,"
        "bad_entry, prod_x:agency:month, price_y:starter:month, price_z:agency:week,"
        "price_sold_now:agency:month",
        sold,
    )
    assert parsed == {
        "price_old_agency": ("agency", -1, "month"),
        "price_old_pro": ("pro", 1000, "year"),
    }


# ─── $0 checkout grants the paid plan ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_fully_discounted_agency_checkout_grants_full_agency_entitlement(
    db, monkeypatch
):
    """$0 charged, Agency granted. Nothing reads the amount, the payment status
    or a PaymentIntent, and none exists for this session."""
    b = _billing()
    sub = _agency_subscription()
    monkeypatch.setattr(b.stripe.Subscription, "retrieve", lambda sid, **kw: sub)
    user = await _trial_user(db)

    await b._handle_checkout_completed(_zero_dollar_session(user.id), db)
    await db.commit()

    user = await _reload(db, user.id)
    assert user.plan == "agency"
    assert user.records_limit == settings.PLAN_LIMITS["agency"] == -1
    assert COUNTY_LIMIT_BY_PLAN[normalize_plan(user.plan)] == -1
    assert RECORD_TYPES_BY_PLAN[normalize_plan(user.plan)] == ALL_RECORD_TYPES
    assert settings.SKIP_TRACE_BUNDLED_QUOTAS[normalize_plan(user.plan)] == 2000
    assert user.stripe_subscription_id == "sub_promo"
    assert user.subscription_status == "active"
    assert user.trial_ends_at is None
    # The trial's 900 records do not follow them onto the paid plan.
    assert user.records_used == 0
    assert user.first_paid_at is not None


@pytest.mark.asyncio
async def test_a_replayed_zero_dollar_checkout_neither_resets_quota_nor_moves_the_window(
    db, monkeypatch
):
    b = _billing()
    sub = _agency_subscription()
    monkeypatch.setattr(b.stripe.Subscription, "retrieve", lambda sid, **kw: sub)
    user = await _trial_user(db)
    session = _zero_dollar_session(user.id)

    await b._handle_checkout_completed(session, db)
    await db.commit()
    user = await _reload(db, user.id)
    user.records_used = 37
    await db.commit()
    first = (user.first_paid_at, user.quota_anchor_at, user.quota_period_start)

    await b._handle_checkout_completed(session, db)
    await db.commit()

    user = await _reload(db, user.id)
    assert user.records_used == 37
    assert (user.first_paid_at, user.quota_anchor_at, user.quota_period_start) == first
    assert user.plan == "agency"


@pytest.mark.asyncio
async def test_the_promotion_ending_changes_nothing_but_the_price(db, monkeypatch):
    """Month 4: Stripe drops the discount and sends an update. Same price, so
    same plan, same counter, same window, and no conversion."""
    b = _billing()
    live = {"sub": _agency_subscription()}
    monkeypatch.setattr(b.stripe.Subscription, "retrieve", lambda sid, **kw: live["sub"])
    user = await _trial_user(db)
    await b._handle_checkout_completed(_zero_dollar_session(user.id), db)
    await db.commit()
    user = await _reload(db, user.id)
    user.records_used = 412
    await db.commit()
    before = (user.first_paid_at, user.quota_anchor_at, user.quota_period_start)

    live["sub"] = _agency_subscription(discounts=[])
    await b._handle_subscription_updated({"id": "sub_promo", "customer": "cus_promo"}, db)
    await db.commit()

    user = await _reload(db, user.id)
    assert user.plan == "agency"
    assert user.records_limit == -1
    assert user.records_used == 412
    assert (user.first_paid_at, user.quota_anchor_at, user.quota_period_start) == before
    assert user.pending_plan is None


@pytest.mark.asyncio
async def test_a_completed_session_on_an_incomplete_subscription_grants_nothing(
    db, monkeypatch
):
    b = _billing()
    sub = _agency_subscription(status="incomplete")
    monkeypatch.setattr(b.stripe.Subscription, "retrieve", lambda sid, **kw: sub)
    user = await _trial_user(db)

    await b._handle_checkout_completed(_zero_dollar_session(user.id), db)
    await db.commit()

    user = await _reload(db, user.id)
    assert user.plan == "pro"
    assert user.records_used == 900
    assert user.first_paid_at is None


# ─── customer.subscription.created ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscription_created_for_an_incomplete_agency_subscription_grants_nothing(
    db, monkeypatch
):
    """Routed through the update handler, an incomplete Agency subscription would
    rank as an upgrade over the trial's 1,000 and grant Agency unpaid."""
    b = _billing()
    sub = _agency_subscription(status="incomplete")
    monkeypatch.setattr(b.stripe.Subscription, "retrieve", lambda sid, **kw: sub)
    user = await _trial_user(db)

    await b._dispatch_stripe_event("customer.subscription.created", sub, db)
    await db.commit()

    user = await _reload(db, user.id)
    assert user.plan == "pro"
    assert user.stripe_subscription_id is None


@pytest.mark.asyncio
async def test_subscription_created_active_converts_when_checkout_completed_is_lost(
    db, monkeypatch
):
    b = _billing()
    sub = _agency_subscription()
    monkeypatch.setattr(b.stripe.Subscription, "retrieve", lambda sid, **kw: sub)
    user = await _trial_user(db)

    await b._dispatch_stripe_event("customer.subscription.created", sub, db)
    await db.commit()

    user = await _reload(db, user.id)
    assert user.plan == "agency"
    assert user.records_limit == -1
    assert user.records_used == 0
    assert user.first_paid_at is not None


@pytest.mark.asyncio
async def test_subscription_created_whose_body_says_active_but_stripe_now_says_incomplete(
    db, monkeypatch
):
    """The event body is stale by the time it is handled; Stripe's answer now
    is what counts."""
    b = _billing()
    body = _agency_subscription(status="active")
    now = _agency_subscription(status="incomplete")
    monkeypatch.setattr(b.stripe.Subscription, "retrieve", lambda sid, **kw: now)
    user = await _trial_user(db)

    await b._dispatch_stripe_event("customer.subscription.created", body, db)
    await db.commit()

    assert (await _reload(db, user.id)).plan == "pro"


@pytest.mark.asyncio
async def test_subscription_created_is_retried_not_trusted_when_stripe_cannot_be_asked(
    db, monkeypatch
):
    b = _billing()

    def _stripe_down(sid, **kw):
        raise b.stripe.error.APIConnectionError("connection reset")

    monkeypatch.setattr(b.stripe.Subscription, "retrieve", _stripe_down)
    user = await _trial_user(db)

    with pytest.raises(b.stripe.error.APIConnectionError):
        await b._dispatch_stripe_event(
            "customer.subscription.created", _agency_subscription(status="active"), db
        )
    uid = user.id
    await db.rollback()

    assert (await _reload(db, uid)).plan == "pro"


@pytest.mark.asyncio
async def test_an_update_for_an_unrecorded_incomplete_subscription_grants_nothing(
    db, monkeypatch
):
    """Checkout now leaves a not-yet-entitled subscription to the update path,
    so that path must not treat an unpaid Agency subscription as an upgrade."""
    b = _billing()
    sub = _agency_subscription(status="incomplete")
    monkeypatch.setattr(b.stripe.Subscription, "retrieve", lambda sid, **kw: sub)
    user = await _trial_user(db)

    await b._handle_subscription_updated({"id": "sub_promo", "customer": "cus_promo"}, db)
    await db.commit()

    user = await _reload(db, user.id)
    assert user.plan == "pro"
    assert user.records_limit == 1000
    assert user.stripe_subscription_id is None


@pytest.mark.asyncio
async def test_the_recorded_subscription_still_reports_past_due(db, monkeypatch):
    """The gate is for subscriptions we have not recorded. Dunning on the
    recorded one must keep flowing."""
    b = _billing()
    sub = _agency_subscription(status="past_due")
    monkeypatch.setattr(b.stripe.Subscription, "retrieve", lambda sid, **kw: sub)
    user = await _trial_user(
        db, plan="agency", records_limit=-1, trial_ends_at=None, records_used=10,
        stripe_subscription_id="sub_promo", subscription_status="active",
        first_paid_at=datetime.now(UTC) - timedelta(days=40),
    )

    await b._handle_subscription_updated({"id": "sub_promo", "customer": "cus_promo"}, db)
    await db.commit()

    user = await _reload(db, user.id)
    assert user.subscription_status == "past_due"
    assert user.entitlement_grace_ends_at is not None
    assert user.plan == "agency"


@pytest.mark.asyncio
async def test_a_hand_set_paying_account_with_no_recorded_id_still_gets_dunning(
    db, monkeypatch
):
    """Plans set by hand carry no stripe_subscription_id. A status change on the
    plan they already pay for grants nothing, so it must still be applied."""
    b = _billing()
    sub = _agency_subscription(status="past_due")
    monkeypatch.setattr(b.stripe.Subscription, "retrieve", lambda sid, **kw: sub)
    monkeypatch.setattr(
        b.stripe.Subscription, "list", lambda **kw: _List([{"id": "sub_promo", "status": "past_due"}])
    )
    user = await _trial_user(
        db, plan="agency", records_limit=-1, trial_ends_at=None,
        stripe_subscription_id=None, first_paid_at=datetime.now(UTC) - timedelta(days=90),
    )

    await b._handle_subscription_updated({"id": "sub_promo", "customer": "cus_promo"}, db)
    await db.commit()

    user = await _reload(db, user.id)
    assert user.subscription_status == "past_due"
    assert user.entitlement_grace_ends_at is not None


@pytest.mark.asyncio
async def test_an_account_with_a_recorded_subscription_never_adopts_another_in_dunning(
    db, monkeypatch
):
    b = _billing()
    sub = _agency_subscription(status="past_due", sub_id="sub_other")
    monkeypatch.setattr(b.stripe.Subscription, "retrieve", lambda sid, **kw: sub)
    monkeypatch.setattr(
        b.stripe.Subscription, "list", lambda **kw: _List([{"id": "sub_other", "status": "past_due"}])
    )
    user = await _trial_user(
        db, plan="agency", records_limit=-1, trial_ends_at=None,
        stripe_subscription_id="sub_promo", subscription_status="active",
        first_paid_at=datetime.now(UTC) - timedelta(days=90),
    )

    await b._handle_subscription_updated({"id": "sub_other", "customer": "cus_promo"}, db)
    await db.commit()

    user = await _reload(db, user.id)
    assert user.stripe_subscription_id == "sub_promo"
    assert user.entitlement_grace_ends_at is None


@pytest.mark.asyncio
async def test_dunning_is_not_bound_when_the_subscription_cannot_be_told_apart(
    db, monkeypatch
):
    b = _billing()
    sub = _agency_subscription(status="past_due")
    monkeypatch.setattr(b.stripe.Subscription, "retrieve", lambda sid, **kw: sub)
    monkeypatch.setattr(
        b.stripe.Subscription, "list",
        lambda **kw: _List([
            {"id": "sub_promo", "status": "past_due"},
            {"id": "sub_twin", "status": "active"},
        ]),
    )
    user = await _trial_user(
        db, plan="agency", records_limit=-1, trial_ends_at=None,
        stripe_subscription_id=None, subscription_status="active",
        first_paid_at=datetime.now(UTC) - timedelta(days=90),
    )

    await b._handle_subscription_updated({"id": "sub_promo", "customer": "cus_promo"}, db)
    await db.commit()

    user = await _reload(db, user.id)
    assert user.stripe_subscription_id is None
    assert user.entitlement_grace_ends_at is None


@pytest.mark.asyncio
async def test_an_incomplete_same_plan_subscription_is_not_adopted_by_a_paying_account(
    db, monkeypatch
):
    """Adopting it would let its later failure or expiry freeze or downgrade a
    customer who is paying on their hand-set plan."""
    b = _billing()
    sub = _agency_subscription(status="incomplete", sub_id="sub_abandoned")
    monkeypatch.setattr(b.stripe.Subscription, "retrieve", lambda sid, **kw: sub)
    user = await _trial_user(
        db, plan="agency", records_limit=-1, trial_ends_at=None,
        stripe_subscription_id=None, subscription_status="active",
        first_paid_at=datetime.now(UTC) - timedelta(days=90),
    )

    await b._handle_subscription_updated({"id": "sub_abandoned", "customer": "cus_promo"}, db)
    await db.commit()

    user = await _reload(db, user.id)
    assert user.stripe_subscription_id is None
    assert user.subscription_status == "active"


@pytest.mark.asyncio
async def test_a_former_payer_on_another_plan_gets_no_unpaid_upgrade(db, monkeypatch):
    """Having paid once is not enough: an incomplete Agency subscription must not
    lift a lapsed Pro payer to Agency."""
    b = _billing()
    sub = _agency_subscription(status="incomplete")
    monkeypatch.setattr(b.stripe.Subscription, "retrieve", lambda sid, **kw: sub)
    user = await _trial_user(
        db, plan="pro", records_limit=1000, trial_ends_at=None, stripe_subscription_id=None,
        first_paid_at=datetime.now(UTC) - timedelta(days=90),
    )

    await b._handle_subscription_updated({"id": "sub_promo", "customer": "cus_promo"}, db)
    await db.commit()

    user = await _reload(db, user.id)
    assert user.plan == "pro"
    assert user.records_limit == 1000


# ─── customer.subscription.deleted ────────────────────────────────────────────

def _listed(sub_id: str, status: str, plan_price: bool = True) -> dict:
    price = _price("agency", "month") if plan_price else "price_not_sold_here"
    return {"id": sub_id, "status": status,
            "items": {"data": [{"id": "si_x", "price": {"id": price}}]}}


@pytest.mark.asyncio
async def test_deleting_a_stray_subscription_does_not_downgrade_the_live_one(
    db, monkeypatch
):
    b = _billing()
    monkeypatch.setattr(
        b.stripe.Subscription, "list",
        # An incomplete stray listed FIRST must not hide the active subscription.
        lambda **kw: _List([
            _listed("sub_other_stray", "incomplete"),
            _listed("sub_promo", "active"),
        ]),
    )
    user = await _trial_user(
        db, plan="agency", records_limit=-1, trial_ends_at=None,
        stripe_subscription_id="sub_promo", subscription_status="active",
    )

    await b._handle_subscription_deleted(
        {"id": "sub_stray", "customer": "cus_promo"}, db
    )
    await db.commit()

    user = await _reload(db, user.id)
    assert user.plan == "agency"
    assert user.stripe_subscription_id == "sub_promo"


@pytest.mark.asyncio
async def test_deleting_the_real_subscription_behind_a_stale_id_still_downgrades(
    db, monkeypatch
):
    b = _billing()
    monkeypatch.setattr(
        b.stripe.Subscription, "list",
        lambda **kw: _List([{"id": "sub_other", "status": "canceled"}]),
    )
    user = await _trial_user(
        db, plan="agency", records_limit=-1, trial_ends_at=None,
        stripe_subscription_id="sub_stale", subscription_status="active",
    )

    await b._handle_subscription_deleted(
        {"id": "sub_other", "customer": "cus_promo"}, db
    )
    await db.commit()

    user = await _reload(db, user.id)
    assert user.plan == "starter"
    assert user.paid_entitlement_ended_at is not None


@pytest.mark.asyncio
async def test_deleting_the_recorded_subscription_rebinds_to_a_surviving_one(
    db, monkeypatch
):
    """Two live subscriptions, the recorded one cancelled: the account stays on
    the survivor's plan instead of dropping to Starter, and no quota is reset."""
    b = _billing()
    monkeypatch.setattr(
        b.stripe.Subscription, "list",
        lambda **kw: _List([_listed("sub_survivor", "active")]),
    )
    monkeypatch.setattr(
        b.stripe.Subscription, "retrieve",
        lambda sid, **kw: _agency_subscription(sub_id=sid, discounts=[]),
    )
    user = await _trial_user(
        db, plan="agency", records_limit=-1, trial_ends_at=None, records_used=77,
        stripe_subscription_id="sub_promo", subscription_status="active",
        first_paid_at=datetime.now(UTC) - timedelta(days=10),
    )

    await b._handle_subscription_deleted({"id": "sub_promo", "customer": "cus_promo"}, db)
    await db.commit()

    user = await _reload(db, user.id)
    assert user.plan == "agency"
    assert user.stripe_subscription_id == "sub_survivor"
    assert user.records_used == 77


@pytest.mark.asyncio
async def test_a_stray_deletion_with_no_recorded_subscription_keeps_an_active_one(
    db, monkeypatch
):
    b = _billing()
    monkeypatch.setattr(
        b.stripe.Subscription, "list",
        lambda **kw: _List([_listed("sub_live", "active")]),
    )
    monkeypatch.setattr(
        b.stripe.Subscription, "retrieve",
        lambda sid, **kw: _agency_subscription(sub_id=sid, discounts=[]),
    )
    user = await _trial_user(
        db, plan="agency", records_limit=-1, trial_ends_at=None,
        stripe_subscription_id=None, first_paid_at=datetime.now(UTC) - timedelta(days=10),
    )

    await b._handle_subscription_deleted({"id": "sub_stray", "customer": "cus_promo"}, db)
    await db.commit()

    assert (await _reload(db, user.id)).plan == "agency"


@pytest.mark.asyncio
async def test_a_survivor_on_a_price_we_do_not_sell_grants_nothing(db, monkeypatch):
    b = _billing()
    monkeypatch.setattr(
        b.stripe.Subscription, "list",
        lambda **kw: _List([_listed("sub_foreign", "active", plan_price=False)]),
    )
    user = await _trial_user(
        db, plan="agency", records_limit=-1, trial_ends_at=None,
        stripe_subscription_id="sub_promo", subscription_status="active",
    )

    await b._handle_subscription_deleted({"id": "sub_promo", "customer": "cus_promo"}, db)
    await db.commit()

    assert (await _reload(db, user.id)).plan == "starter"


@pytest.mark.asyncio
async def test_a_deletion_is_retried_when_stripe_cannot_say_what_remains(db, monkeypatch):
    b = _billing()

    def _stripe_down(**kw):
        raise b.stripe.error.APIConnectionError("connection reset")

    monkeypatch.setattr(b.stripe.Subscription, "list", _stripe_down)
    user = await _trial_user(
        db, plan="agency", records_limit=-1, trial_ends_at=None,
        stripe_subscription_id="sub_promo", subscription_status="active",
    )

    with pytest.raises(b.stripe.error.APIConnectionError):
        await b._handle_subscription_deleted({"id": "sub_promo", "customer": "cus_promo"}, db)
    uid = user.id
    await db.rollback()

    assert (await _reload(db, uid)).plan == "agency"


@pytest.mark.asyncio
async def test_cancelling_the_promotional_subscription_downgrades_normally(db, monkeypatch):
    b = _billing()
    monkeypatch.setattr(
        b.stripe.Subscription, "list",
        lambda **kw: _List([_listed("sub_promo", "canceled")]),
    )
    user = await _trial_user(
        db, plan="agency", records_limit=-1, trial_ends_at=None, records_used=55,
        stripe_subscription_id="sub_promo", subscription_status="active",
    )

    await b._handle_subscription_deleted({"id": "sub_promo", "customer": "cus_promo"}, db)
    await db.commit()

    user = await _reload(db, user.id)
    assert user.plan == "starter"
    assert user.records_limit == settings.PLAN_LIMITS["starter"]
    assert user.records_used == 55
    assert user.stripe_subscription_id is None


# ─── Webhook ledger: recorded and applied commit together ─────────────────────

def _signed(event: dict) -> tuple[bytes, str]:
    payload = json.dumps(event).encode()
    ts = int(time.time())
    sig = hmac.new(
        _WEBHOOK_SECRET.encode(), f"{ts}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return payload, f"t={ts},v1={sig}"


def _checkout_event(user_id: str, event_id: str) -> dict:
    return {
        "id": event_id,
        "object": "event",
        "type": "checkout.session.completed",
        "data": {"object": _zero_dollar_session(user_id)},
    }


async def _ledger_row(db: AsyncSession, event_id: str):
    await db.rollback()
    return (
        await db.execute(
            text("SELECT event_type FROM stripe_webhook_events WHERE event_id = :e"),
            {"e": event_id},
        )
    ).first()


@pytest.mark.asyncio
async def test_a_failed_webhook_handler_leaves_no_record_so_the_retry_activates(
    client, db, monkeypatch
):
    b = _billing()
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    user = await _trial_user(db)
    uid = user.id
    event_id = f"evt_{uuid.uuid4().hex}"
    payload, sig = _signed(_checkout_event(uid, event_id))

    def _stripe_down(sid, **kw):
        raise b.stripe.error.APIConnectionError("connection reset")

    monkeypatch.setattr(b.stripe.Subscription, "retrieve", _stripe_down)
    try:
        first = await client.post(
            "/billing/webhook", content=payload, headers={"stripe-signature": sig}
        )
        assert first.status_code >= 500
    except b.stripe.error.APIConnectionError:
        pass  # the transport re-raised the app exception: also a failed delivery

    assert await _ledger_row(db, event_id) is None, "a failed attempt was recorded"

    sub = _agency_subscription()
    monkeypatch.setattr(b.stripe.Subscription, "retrieve", lambda sid, **kw: sub)
    retry = await client.post(
        "/billing/webhook", content=payload, headers={"stripe-signature": sig}
    )
    assert retry.status_code == 200, retry.text
    assert tuple(await _ledger_row(db, event_id)) == ("checkout.session.completed",)
    assert (await _reload(db, uid)).plan == "agency"


@pytest.mark.asyncio
async def test_a_recorded_event_is_acknowledged_without_running_its_handler_again(
    client, db, monkeypatch
):
    b = _billing()
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    user = await _trial_user(db)
    uid = user.id
    event_id = f"evt_{uuid.uuid4().hex}"
    await db.execute(
        text("INSERT INTO stripe_webhook_events (event_id, event_type) VALUES (:e, :t)"),
        {"e": event_id, "t": "checkout.session.completed"},
    )
    await db.commit()

    def _must_not_run(sid, **kw):
        raise AssertionError("a recorded event must not be handled again")

    monkeypatch.setattr(b.stripe.Subscription, "retrieve", _must_not_run)
    payload, sig = _signed(_checkout_event(uid, event_id))
    resp = await client.post(
        "/billing/webhook", content=payload, headers={"stripe-signature": sig}
    )

    assert resp.status_code == 200, resp.text
    assert (await _reload(db, uid)).plan == "pro"


@pytest.mark.asyncio
async def test_a_handler_row_lock_is_not_bound_by_the_event_lock_timeout(
    client, db, monkeypatch
):
    """lock_timeout is for the event lock only; ordinary row contention must wait."""
    b = _billing()
    monkeypatch.setattr(b, "_WEBHOOK_LOCK_TIMEOUT", "1s")
    captured: list = []

    async def _spy(event_type, data, session):
        captured.append((await session.execute(text("SHOW lock_timeout"))).scalar())

    monkeypatch.setattr(b, "_dispatch_stripe_event", _spy)
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", _WEBHOOK_SECRET)

    event = {"id": f"evt_{uuid.uuid4().hex}", "object": "event",
             "type": "invoice.payment_failed", "data": {"object": {}}}
    payload, sig = _signed(event)
    resp = await client.post(
        "/billing/webhook", content=payload, headers={"stripe-signature": sig}
    )

    assert resp.status_code == 200, resp.text
    assert captured == ["0"], captured


@pytest.mark.asyncio
async def test_a_failed_post_create_recheck_expires_the_new_session(client, db, monkeypatch):
    b = _billing()
    created: list = []
    expired: list = []
    _patch_checkout(monkeypatch, b, created=created, expired=expired,
                    subscription_reads=[[], []])
    calls = {"n": 0}
    real_subs = b.stripe.Subscription.list

    def _third_read_fails(**kw):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise b.stripe.error.APIConnectionError("connection reset")
        return real_subs(**kw)

    monkeypatch.setattr(b.stripe.Subscription, "list", _third_read_fails)
    _user, token = await _starter_with_token(db)

    r = await client.post(
        "/billing/checkout",
        json={"price_id": _price("agency", "month")},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 503, r.text
    assert expired == ["cs_test_1"]


@pytest.mark.asyncio
async def test_a_duplicate_delivery_while_the_first_holds_the_event_lock_gets_409(
    client, db, monkeypatch
):
    """A 200 here would mark the event delivered while the first attempt can
    still roll back, and Stripe would never retry it."""
    b = _billing()
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    monkeypatch.setattr(b, "_WEBHOOK_LOCK_TIMEOUT", "1s")
    user = await _trial_user(db)
    uid = user.id
    event_id = f"evt_{uuid.uuid4().hex}"

    from src.db import session as dbs

    # Another in-flight delivery of the same event, holding its lock.
    async with dbs.AsyncSessionLocal() as holder:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(4244, hashtext(:e))"), {"e": event_id}
        )
        payload, sig = _signed(_checkout_event(uid, event_id))
        resp = await client.post(
            "/billing/webhook", content=payload, headers={"stripe-signature": sig}
        )
        await holder.rollback()

    assert resp.status_code == 409, resp.text
    assert await _ledger_row(db, event_id) is None
    assert (await _reload(db, uid)).plan == "pro"


# ─── Checkout: codes for everyone, one subscription per customer ──────────────

def _patch_checkout(monkeypatch, b, *, created, subscription_reads=None, expired=None):
    reads = iter(subscription_reads or [])

    def _subs(**kw):
        return _List(next(reads, []))

    def _expire(sid, **kw):
        if expired is not None:
            expired.append(sid)
        return {"id": sid, "status": "expired"}

    monkeypatch.setattr(b.stripe.Customer, "list", lambda **kw: _List([]))
    monkeypatch.setattr(b.stripe.Customer, "create", lambda **kw: {"id": "cus_promo"})
    monkeypatch.setattr(b.stripe.Subscription, "list", _subs)
    monkeypatch.setattr(b.stripe.checkout.Session, "list", lambda **kw: _List([]))
    monkeypatch.setattr(b.stripe.checkout.Session, "expire", _expire)

    class _Session(dict):
        @property
        def url(self):
            return self["url"]

    def _create(**kwargs):
        created.append(kwargs)
        return _Session(id=f"cs_test_{len(created)}", url="https://checkout.example/session")

    monkeypatch.setattr(b.stripe.checkout.Session, "create", _create)


async def _starter_with_token(db: AsyncSession) -> tuple[User, str]:
    user = await _trial_user(
        db, plan="starter", records_limit=50, records_used=0, trial_ends_at=None
    )
    return user, create_secure_token(user.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan", "interval"),
    [("pro", "month"), ("pro", "year"), ("business", "year"), ("agency", "month"),
     ("agency", "year")],
)
async def test_every_paid_checkout_offers_the_code_box(
    client, db, monkeypatch, plan, interval
):
    """FOUNDING25 stays enterable on every plan and interval. Which products a
    code may discount is decided by its coupon in Stripe, not by hiding the box."""
    b = _billing()
    created: list = []
    _patch_checkout(monkeypatch, b, created=created)
    _user, token = await _starter_with_token(db)

    r = await client.post(
        "/billing/checkout",
        json={"price_id": _price(plan, interval)},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 200, r.text
    assert created[0]["allow_promotion_codes"] is True
    assert created[0]["payment_method_collection"] == "always"
    assert "discounts" not in created[0], "the backend must never apply a discount itself"


@pytest.mark.asyncio
async def test_a_subscription_that_appears_after_the_session_is_created_expires_it(
    client, db, monkeypatch
):
    """An older Session paid in the gap: the new one must not survive to be paid too."""
    b = _billing()
    created: list = []
    expired: list = []
    _patch_checkout(
        monkeypatch, b, created=created, expired=expired,
        subscription_reads=[[], [], [{"id": "sub_paid_in_gap", "status": "active"}]],
    )
    _user, token = await _starter_with_token(db)

    r = await client.post(
        "/billing/checkout",
        json={"price_id": _price("agency", "month")},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "subscription_exists"
    assert expired == ["cs_test_1"]


@pytest.mark.asyncio
async def test_a_legacy_plan_price_is_recognised_but_never_sold(client, db, monkeypatch):
    b = _billing()
    monkeypatch.setitem(b._PRICE_TO_PLAN, "price_legacy_agency", ("agency", -1, "month"))
    monkeypatch.setitem(
        b._LEGACY_PRICE_TO_PLAN, "price_legacy_agency", ("agency", -1, "month")
    )
    created: list = []
    _patch_checkout(monkeypatch, b, created=created)
    _user, token = await _starter_with_token(db)

    r = await client.post(
        "/billing/checkout",
        json={"price_id": "price_legacy_agency"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
    assert created == []

    r = await client.post(
        "/billing/change-plan",
        json={"price_id": "price_legacy_agency"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400

    # ...while an existing subscription on it still maps to Agency.
    legacy_sub = {
        "id": "sub_legacy", "customer": "cus_legacy_owner", "status": "active",
        "items": {"data": [{"id": "si_x", "price": {"id": "price_legacy_agency"}}]},
        "billing_cycle_anchor": int(time.time()), "cancel_at_period_end": False,
        "current_period_end": int(time.time()) + 86400, "discounts": [],
    }
    monkeypatch.setattr(b.stripe.Subscription, "retrieve", lambda sid, **kw: legacy_sub)
    user = await _trial_user(db, stripe_customer_id="cus_legacy_owner")
    uid = user.id
    await b._handle_subscription_updated(
        {"id": "sub_legacy", "customer": "cus_legacy_owner"}, db
    )
    await db.commit()
    assert (await _reload(db, uid)).plan == "agency"


@pytest.mark.asyncio
async def test_an_annual_subscription_carrying_the_single_customer_coupon_pages_ops(
    db, monkeypatch, caplog
):
    b = _billing()
    tagged = {**_COUPON_3_MONTHS, "metadata": {"bridgeleads_resource": "single_customer_promo"}}
    sub = _agency_subscription(
        interval="year",
        discounts=[{"id": "di_promo", "coupon": tagged, "end": int(time.time()) + 86400}],
    )
    monkeypatch.setattr(b.stripe.Subscription, "retrieve", lambda sid, **kw: sub)
    await _trial_user(db)

    with caplog.at_level("ERROR"):
        await b._handle_subscription_updated({"id": "sub_promo", "customer": "cus_promo"}, db)
    await db.commit()

    assert any("single-customer promotion coupon" in r.getMessage() for r in caplog.records)


# ─── Invoice webhooks on current Stripe API versions ──────────────────────────

def test_the_invoice_subscription_is_read_on_old_and_new_api_versions():
    b = _billing()
    assert b._invoice_subscription_id({"subscription": "sub_old"}) == "sub_old"
    assert b._invoice_subscription_id(
        {"parent": {"subscription_details": {"subscription": "sub_new"}}}
    ) == "sub_new"
    assert b._invoice_subscription_id(
        {"parent": {"subscription_details": {"subscription": {"id": "sub_exp"}}}}
    ) == "sub_exp"
    assert b._invoice_subscription_id({"parent": None}) is None


async def _dunning_customer(db: AsyncSession) -> User:
    """An Agency payer 95 days in: the promotion's first real charge is due."""
    return await _trial_user(
        db, plan="agency", records_limit=-1, trial_ends_at=None,
        stripe_subscription_id="sub_promo", subscription_status="active",
        first_paid_at=datetime.now(UTC) - timedelta(days=95),
    )


def _renewal_invoice(**overrides) -> dict:
    invoice = {
        "id": "in_month4", "customer": "cus_promo", "attempt_count": 1,
        "parent": {"type": "subscription_details",
                   "subscription_details": {"subscription": "sub_promo"}},
    }
    invoice.update(overrides)
    return invoice


def _stripe_now(monkeypatch, b, *, invoice_status: str, subscription_status: str):
    """Stripe's CURRENT answer for the invoice and subscription; returns the reads."""
    reads: list = []

    def _invoice(iid, **kw):
        reads.append(("invoice", iid))
        return {"id": iid, "status": invoice_status}

    def _subscription(sid, **kw):
        reads.append(("subscription", sid))
        return _agency_subscription(status=subscription_status, sub_id=sid)

    monkeypatch.setattr(b.stripe.Invoice, "retrieve", _invoice)
    monkeypatch.setattr(b.stripe.Subscription, "retrieve", _subscription)
    return reads


def _capture_notifications(monkeypatch) -> list:
    import src.workers.delivery as delivery
    import src.workers.tasks as worker_tasks

    sent: list = []
    monkeypatch.setattr(
        delivery, "_send_payment_failed_email", lambda email, n: sent.append(("email", n))
    )
    monkeypatch.setattr(
        worker_tasks.emit_payment_notification, "delay",
        lambda uid, n: sent.append(("in_app", n)),
    )
    return sent


@pytest.mark.asyncio
async def test_dunning_starts_from_a_new_api_version_invoice_and_recovery_clears_it(
    db, monkeypatch
):
    """Month 4 is the promotion's first real charge. On an endpoint rendering
    2025-03-31+ payloads there is no top-level invoice.subscription, and reading
    only that field left a failed renewal with no grace and no freeze. Recovery
    arrives as customer.subscription.updated, past_due -> active."""
    b = _billing()
    sent = _capture_notifications(monkeypatch)
    _stripe_now(monkeypatch, b, invoice_status="open", subscription_status="past_due")
    uid = (await _dunning_customer(db)).id

    await b._handle_payment_failed(_renewal_invoice(), db)
    await db.commit()
    user = await _reload(db, uid)
    assert user.subscription_status == "past_due"
    assert user.entitlement_grace_ends_at is not None
    assert sent == [("email", 1), ("in_app", 1)]

    _stripe_now(monkeypatch, b, invoice_status="paid", subscription_status="active")
    await b._handle_subscription_updated({"id": "sub_promo", "customer": "cus_promo"}, db)
    await db.commit()
    user = await _reload(db, uid)
    assert user.subscription_status == "active"
    assert user.entitlement_grace_ends_at is None
    assert user.plan == "agency" and user.records_limit == -1


@pytest.mark.asyncio
async def test_a_late_failure_for_a_since_paid_invoice_changes_nothing_and_says_nothing(
    db, monkeypatch
):
    """Stripe does not order deliveries. The failure for retry 1 can land after
    retry 2 succeeded and the recovery was applied; trusting its body re-froze a
    paying customer and emailed them about a payment that went through."""
    b = _billing()
    sent = _capture_notifications(monkeypatch)
    _stripe_now(monkeypatch, b, invoice_status="paid", subscription_status="active")
    uid = (await _dunning_customer(db)).id

    await b._handle_payment_failed(_renewal_invoice(), db)
    await db.commit()

    user = await _reload(db, uid)
    assert user.subscription_status == "active"
    assert user.entitlement_grace_ends_at is None
    assert sent == []


@pytest.mark.asyncio
async def test_an_unpaid_subscription_records_unpaid_not_past_due(db, monkeypatch):
    b = _billing()
    _capture_notifications(monkeypatch)
    _stripe_now(monkeypatch, b, invoice_status="open", subscription_status="unpaid")
    uid = (await _dunning_customer(db)).id

    await b._handle_payment_failed(_renewal_invoice(), db)
    await db.commit()

    user = await _reload(db, uid)
    assert user.subscription_status == "unpaid"
    assert user.entitlement_grace_ends_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", [
    None,
    {"type": "subscription_details", "subscription_details": {"subscription": "sub_other"}},
])
async def test_a_failure_off_the_recorded_subscription_notifies_but_never_freezes(
    db, monkeypatch, parent
):
    """A one-off invoice, or one for a subscription the account does not hold."""
    b = _billing()
    sent = _capture_notifications(monkeypatch)
    reads = _stripe_now(monkeypatch, b, invoice_status="open", subscription_status="past_due")
    uid = (await _dunning_customer(db)).id

    await b._handle_payment_failed(_renewal_invoice(parent=parent), db)
    await db.commit()

    user = await _reload(db, uid)
    assert user.subscription_status == "active"
    assert user.entitlement_grace_ends_at is None
    assert [r for r in reads if r[0] == "subscription"] == []
    assert sent == [("email", 1), ("in_app", 1)]


@pytest.mark.asyncio
async def test_a_failure_stripe_cannot_confirm_is_retried_not_recorded(
    client, db, monkeypatch
):
    b = _billing()
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    sent = _capture_notifications(monkeypatch)
    uid = (await _dunning_customer(db)).id
    event_id = f"evt_{uuid.uuid4().hex}"
    payload, sig = _signed({
        "id": event_id, "object": "event", "type": "invoice.payment_failed",
        "data": {"object": _renewal_invoice()},
    })

    def _stripe_down(iid, **kw):
        raise b.stripe.error.APIConnectionError("connection reset")

    monkeypatch.setattr(b.stripe.Invoice, "retrieve", _stripe_down)
    try:
        first = await client.post(
            "/billing/webhook", content=payload, headers={"stripe-signature": sig}
        )
        assert first.status_code >= 500
    except b.stripe.error.APIConnectionError:
        pass  # the transport re-raised the app exception: also a failed delivery

    assert await _ledger_row(db, event_id) is None, "an unconfirmed failure was recorded"
    user = await _reload(db, uid)
    assert user.entitlement_grace_ends_at is None and user.subscription_status == "active"
    assert sent == []

    _stripe_now(monkeypatch, b, invoice_status="open", subscription_status="past_due")
    retry = await client.post(
        "/billing/webhook", content=payload, headers={"stripe-signature": sig}
    )
    assert retry.status_code == 200, retry.text
    assert tuple(await _ledger_row(db, event_id)) == ("invoice.payment_failed",)
    assert (await _reload(db, uid)).entitlement_grace_ends_at is not None


def _row_is_locked(user_id: str) -> bool:
    """Whether another transaction holds this user's row lock, asked from outside."""
    from sqlalchemy.exc import OperationalError

    from src.db.session import SyncSessionLocal

    with SyncSessionLocal() as other:
        try:
            other.execute(
                text("SELECT 1 FROM users WHERE id = CAST(:u AS uuid) FOR UPDATE NOWAIT"),
                {"u": str(user_id)},
            )
            return False
        except OperationalError:
            return True
        finally:
            other.rollback()


@pytest.mark.asyncio
async def test_a_failure_event_without_an_invoice_id_is_refused_not_trusted(db, monkeypatch):
    b = _billing()
    sent = _capture_notifications(monkeypatch)
    reads = _stripe_now(monkeypatch, b, invoice_status="open", subscription_status="past_due")
    uid = (await _dunning_customer(db)).id

    with pytest.raises(ValueError):
        await b._handle_payment_failed(_renewal_invoice(id=None), db)
    await db.rollback()

    user = await _reload(db, uid)
    assert user.entitlement_grace_ends_at is None and user.subscription_status == "active"
    assert reads == [] and sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event", ["subscription_updated", "subscription_deleted", "payment_failed"]
)
async def test_the_user_row_is_locked_before_stripe_is_asked(db, monkeypatch, event):
    """Locked AFTER the read, a slow read of an older state could take the lock
    second and overwrite what a newer delivery had just written."""
    b = _billing()
    _capture_notifications(monkeypatch)
    uid = (await _dunning_customer(db)).id
    locked_at_read: list = []

    def _invoice(iid, **kw):
        locked_at_read.append(_row_is_locked(uid))
        return {"id": iid, "status": "open"}

    def _subscription(sid, **kw):
        locked_at_read.append(_row_is_locked(uid))
        return _agency_subscription(status="past_due", sub_id=sid)

    def _subscriptions(**kw):
        locked_at_read.append(_row_is_locked(uid))
        return _List([])

    monkeypatch.setattr(b.stripe.Invoice, "retrieve", _invoice)
    monkeypatch.setattr(b.stripe.Subscription, "retrieve", _subscription)
    monkeypatch.setattr(b.stripe.Subscription, "list", _subscriptions)

    if event == "payment_failed":
        await b._handle_payment_failed(_renewal_invoice(), db)
    elif event == "subscription_deleted":
        await b._handle_subscription_deleted({"id": "sub_promo", "customer": "cus_promo"}, db)
    else:
        await b._handle_subscription_updated({"id": "sub_promo", "customer": "cus_promo"}, db)
    await db.commit()

    assert locked_at_read and all(locked_at_read), locked_at_read
