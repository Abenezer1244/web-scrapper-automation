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

    resp = await client.post(
        "/billing/change-plan",
        json={"price_id": agency_m},
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
