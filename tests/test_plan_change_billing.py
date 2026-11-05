"""Plan-change billing must not let a customer hold a tier they have not paid for.

Audit 2026-09-25, N-02 (P1). change-plan used ``create_prorations`` for every
move. The upgrade charge then waited for the next invoice while the app granted
the bigger tier at once, and a downgrade was credited back by Stripe at once
while the app kept the bigger tier until the quota boundary. Pro -> Agency ->
Pro therefore bought Agency for the rest of the period at about the Pro price,
every period.

The rule now: an upgrade (or a same-tier interval move) is invoiced and paid
BEFORE Stripe changes the subscription, and a downgrade is never credited. The
Stripe SDK is patched at its boundary only: it is a paid external API.
"""
from __future__ import annotations

import uuid

import pytest
import stripe
from sqlalchemy import select

from src.api.auth import create_secure_token, hash_password
from src.api.routes import billing as b
from src.db.models import User


def _prices():
    s = b.settings
    if not (s.STRIPE_PRICE_PRO and s.STRIPE_PRICE_AGENCY and s.STRIPE_PRICE_PRO_ANNUAL):
        pytest.skip("no Stripe plan prices configured in this environment")
    return s.STRIPE_PRICE_PRO, s.STRIPE_PRICE_PRO_ANNUAL, s.STRIPE_PRICE_AGENCY


def test_an_upgrade_is_paid_before_the_subscription_changes():
    pro_m, _, agency_m = _prices()
    kwargs = b._plan_change_billing(pro_m, agency_m)
    assert kwargs["proration_behavior"] == "always_invoice"
    assert kwargs["payment_behavior"] == "error_if_incomplete"


def test_a_downgrade_is_never_credited():
    pro_m, _, agency_m = _prices()
    kwargs = b._plan_change_billing(agency_m, pro_m)
    assert kwargs["proration_behavior"] == "none", (
        "a credited downgrade while the app keeps the bigger tier until the "
        "boundary is the arbitrage"
    )
    assert kwargs["payment_behavior"] == "error_if_incomplete"


def test_a_same_tier_interval_move_is_charged_now():
    pro_m, pro_y, _ = _prices()
    kwargs = b._plan_change_billing(pro_m, pro_y)
    assert kwargs["proration_behavior"] == "always_invoice"


def test_the_arbitrage_round_trip_costs_the_upgrade():
    """Pro -> Agency -> Pro: the upgrade leg charges now, the downgrade leg
    gives nothing back. Neither leg may be create_prorations."""
    pro_m, _, agency_m = _prices()
    legs = [b._plan_change_billing(pro_m, agency_m), b._plan_change_billing(agency_m, pro_m)]
    assert all(leg["proration_behavior"] != "create_prorations" for leg in legs)


@pytest.mark.asyncio
async def test_a_declined_upgrade_is_402_and_changes_nothing(db, client, monkeypatch):
    pro_m, _, agency_m = _prices()
    user = User(
        id=str(uuid.uuid4()),
        email=f"planchg_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"),
        plan="pro", records_used=0, records_limit=1000,
        stripe_customer_id="cus_planchg", stripe_subscription_id="sub_live",
        subscription_status="active",
    )
    db.add(user)
    await db.commit()

    live = {
        "id": "sub_live", "status": "active", "current_period_start": None,
        "items": {"data": [{"id": "si_0", "price": {"id": pro_m}}]},
    }
    monkeypatch.setattr(b, "_live_subscription", lambda customer_id: live)
    sent = {}

    def _declined(sub_id, **kwargs):
        sent.update(kwargs)
        raise stripe.error.CardError("Your card was declined.", param=None, code="card_declined")

    monkeypatch.setattr(b.stripe.Subscription, "modify", _declined)
    # change-plan now verifies the target Price against the catalog first; answer
    # as a correctly configured account would.
    from src.config.plans import get_plan

    def _price(pid, **kw):
        if pid not in b._PRICE_TO_PLAN:  # the target plan's metered overage Price
            return {"unit_amount": 5, "currency": "usd", "billing_scheme": "per_unit",
                    "recurring": {"interval": "month", "interval_count": 1,
                                  "usage_type": "metered"}}
        plan_id, _limit, interval = b._PRICE_TO_PLAN[pid]
        plan = get_plan(plan_id)
        amount = (plan["price_annual"] if interval == "year" else plan["price_monthly"]) * 100
        return {"unit_amount": amount, "currency": "usd", "billing_scheme": "per_unit",
                "recurring": {"interval": interval, "interval_count": 1, "usage_type": "licensed"}}

    monkeypatch.setattr(b.stripe.Price, "retrieve", _price)

    resp = await client.post(
        "/billing/change-plan",
        json={"price_id": agency_m, "expected_price": get_plan("agency")["price_monthly"]},
        headers={"Authorization": f"Bearer {create_secure_token(user.id)}"},
    )
    assert resp.status_code == 402, resp.text
    assert "sk_" not in resp.text and "Traceback" not in resp.text
    assert sent["proration_behavior"] == "always_invoice"
    assert sent["payment_behavior"] == "error_if_incomplete"

    plan, limit = (
        await db.execute(select(User.plan, User.records_limit).where(User.id == user.id))
    ).one()
    assert (plan, limit) == ("pro", 1000)


@pytest.mark.asyncio
async def test_a_switch_at_a_price_the_customer_was_not_shown_is_refused(db, client, monkeypatch):
    """The dialog sends the amount it showed; a changed price is a 409, not a charge
    (compliance audit 2026-11, Phase 5c)."""
    from src.config.plans import get_plan

    pro_m, _, agency_m = _prices()
    user = User(
        id=str(uuid.uuid4()),
        email=f"planchg_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"),
        plan="pro", records_used=0, records_limit=1000,
        stripe_customer_id="cus_planchg", stripe_subscription_id="sub_live",
        subscription_status="active",
    )
    db.add(user)
    await db.commit()
    live = {"id": "sub_live", "status": "active", "current_period_start": None,
            "items": {"data": [{"id": "si_0", "price": {"id": pro_m}}]}}
    monkeypatch.setattr(b, "_live_subscription", lambda customer_id: live)
    modified = []
    monkeypatch.setattr(b.stripe.Subscription, "modify",
                        lambda sub_id, **kw: modified.append(kw) or {"id": sub_id})

    def _price(pid, **kw):
        if pid not in b._PRICE_TO_PLAN:
            return {"unit_amount": 5, "currency": "usd", "billing_scheme": "per_unit",
                    "recurring": {"interval": "month", "interval_count": 1, "usage_type": "metered"}}
        plan_id, _limit, interval = b._PRICE_TO_PLAN[pid]
        plan = get_plan(plan_id)
        amount = (plan["price_annual"] if interval == "year" else plan["price_monthly"]) * 100
        return {"unit_amount": amount, "currency": "usd", "billing_scheme": "per_unit",
                "recurring": {"interval": interval, "interval_count": 1, "usage_type": "licensed"}}

    monkeypatch.setattr(b.stripe.Price, "retrieve", _price)
    shown = get_plan("agency")["price_monthly"] - 100  # a stale amount the dialog showed

    resp = await client.post(
        "/billing/change-plan",
        json={"price_id": agency_m, "expected_price": shown},
        headers={"Authorization": f"Bearer {create_secure_token(user.id)}"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "price_changed"
    assert modified == []  # nothing was charged or changed


@pytest.mark.asyncio
async def test_rollout_grace_a_switch_without_expected_price_reaches_stripe(
        db, client, monkeypatch):
    """ROLLOUT GRACE (removed by the follow-up PR, which restores the 409): the
    live frontend omits expected_price for the minutes until its own deploy, so a
    missing value proceeds, but only after the strict catalog-vs-Stripe check."""
    from src.config.plans import get_plan

    pro_m, _, agency_m = _prices()
    user = User(
        id=str(uuid.uuid4()),
        email=f"planchg_{uuid.uuid4().hex[:8]}@test.bridgeleads.io",
        password_hash=hash_password("TestPass123!"),
        plan="pro", records_used=0, records_limit=1000,
        stripe_customer_id="cus_planchg", stripe_subscription_id="sub_live",
        subscription_status="active",
    )
    db.add(user)
    await db.commit()
    live = {
        "id": "sub_live", "status": "active", "current_period_start": None,
        "items": {"data": [{"id": "si_0", "price": {"id": pro_m}}]},
    }
    monkeypatch.setattr(b, "_live_subscription", lambda customer_id: live)
    modified = []

    def _declined(sub_id, **kw):
        # Reaching Stripe at all is the point; decline so nothing else is needed.
        modified.append(kw)
        raise stripe.error.CardError("Your card was declined.", param=None, code="card_declined")

    monkeypatch.setattr(b.stripe.Subscription, "modify", _declined)

    def _price(pid, **kw):
        if pid not in b._PRICE_TO_PLAN:
            return {"unit_amount": 5, "currency": "usd", "billing_scheme": "per_unit",
                    "recurring": {"interval": "month", "interval_count": 1, "usage_type": "metered"}}
        plan_id, _limit, interval = b._PRICE_TO_PLAN[pid]
        plan = get_plan(plan_id)
        amount = (plan["price_annual"] if interval == "year" else plan["price_monthly"]) * 100
        return {"unit_amount": amount, "currency": "usd", "billing_scheme": "per_unit",
                "recurring": {"interval": interval, "interval_count": 1, "usage_type": "licensed"}}

    monkeypatch.setattr(b.stripe.Price, "retrieve", _price)
    resp = await client.post(
        "/billing/change-plan",
        json={"price_id": agency_m},
        headers={"Authorization": f"Bearer {create_secure_token(user.id)}"},
    )
    assert resp.status_code == 402, resp.text  # got past the price checks to Stripe
    assert len(modified) == 1
