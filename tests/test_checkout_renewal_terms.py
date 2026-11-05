"""The automatic-renewal disclosure shown on Stripe Checkout (ROSCA).

Compliance audit 2026-11, Phase 5a. The wording describes the Prices Stripe will
actually charge (read with Price.retrieve), not the catalog: _renewal_terms is
pure over those values, and _checkout_renewal_terms reads them, fails closed when
it cannot, and pages ops when Stripe and the catalog disagree. Stripe is observed
with pytest's monkeypatch at the SDK boundary.
"""

import pytest
from fastapi import HTTPException

import src.api.routes.billing as billing
from src.api.routes.billing import _format_money, _renewal_terms
from src.config import settings
from src.config.plans import get_plan

_PAID = ("pro", "business", "agency")


def _price(amount: int, interval: str = "month", currency: str = "usd",
           count: int = 1, per_unit: bool = True) -> dict:
    return {"unit_amount": amount, "currency": currency, "interval": interval,
            "interval_count": count, "per_unit": per_unit}


def test_states_the_recurring_charge_stripe_will_bill():
    text = _renewal_terms("Pro", _price(19900), 250, _price(8))
    assert "renews automatically every month for $199.00 USD" in text
    assert "until you cancel or the subscription otherwise ends" in text
    assert "It includes 250 contact lookups each month; additional lookups cost $0.08 USD each." in text
    assert "To cancel, open Billing in your BridgeLeads settings." in text
    annual = _renewal_terms("Pro", _price(191000, "year"), 250, None)
    assert "every year for $1,910.00 USD" in annual


def test_a_multi_period_or_zero_decimal_price_is_described_as_stripe_bills_it():
    assert "every 3 months for" in _renewal_terms("Pro", _price(50000, count=3), 0, None)
    assert "for 1,234 JPY" in _renewal_terms("Pro", _price(1234, currency="jpy"), 0, None)


def test_no_overage_rate_without_a_per_unit_metered_price_on_the_session():
    assert "additional lookups" not in _renewal_terms("Pro", _price(19900), 250, None)
    tiered = _price(8, per_unit=False)
    assert "additional lookups" not in _renewal_terms("Pro", _price(19900), 250, tiered)
    assert "contact lookups each month" in _renewal_terms("Pro", _price(19900), 250, None)


@pytest.mark.parametrize("plan_id", _PAID)
@pytest.mark.parametrize("interval", ["month", "year"])
def test_fits_stripe_limits_and_house_style(plan_id, interval):
    plan = get_plan(plan_id)
    amount = (plan["price_annual"] if interval == "year" else plan["price_monthly"]) * 100
    text = _renewal_terms(plan["name"], _price(amount, interval),
                          settings.SKIP_TRACE_BUNDLED_QUOTAS[plan_id], _price(8))
    assert len(text) <= 1200          # Stripe custom_text maximum
    assert "—" not in text       # no em dashes in user-facing copy
    for unvouched in ("anytime", "end of the", "refund", "less any promotion"):
        assert unvouched not in text.lower(), unvouched


def test_money_formatting_respects_zero_decimal_currencies():
    assert _format_money(191000, "usd") == "$1,910.00 USD"
    assert _format_money(1234, "jpy") == "1,234 JPY"
    assert _format_money(1234, "eur") == "12.34 EUR"


# ─── Reading the Prices from Stripe ───────────────────────────────────────────

def _stripe_price(amount, interval="month", currency="usd", scheme="per_unit",
                  usage="licensed", count=1):
    return {"unit_amount": amount, "currency": currency, "billing_scheme": scheme,
            "recurring": {"interval": interval, "interval_count": count, "usage_type": usage}}


@pytest.fixture
def stripe_prices(monkeypatch):
    """Price.retrieve answers from this dict; alerts are captured, deduper reset."""
    prices: dict = {}
    alerts: list = []
    monkeypatch.setattr(billing.stripe.Price, "retrieve", lambda pid, **kw: prices[pid])
    monkeypatch.setattr(billing, "_alert_billing_gap",
                        lambda reason, key, **ctx: alerts.append((key, ctx)))
    monkeypatch.setattr(billing, "_CATALOG_MISMATCH_ALERTED", set())
    return prices, alerts


def test_checkout_terms_use_stripes_amount_not_the_catalogs(stripe_prices):
    prices, alerts = stripe_prices
    prices.update(price_plan=_stripe_price(19900), price_metered=_stripe_price(5, usage="metered"))
    text = billing._checkout_renewal_terms("pro", "price_plan", "month", "price_metered")
    assert "$199.00 USD" in text and "$0.05 USD each" in text
    assert alerts == []  # matches the catalog


def test_a_catalog_mismatch_pages_ops_once_per_price_and_still_discloses_stripes(stripe_prices):
    prices, alerts = stripe_prices
    prices.update(price_plan=_stripe_price(24900))
    for _ in range(3):
        text = billing._checkout_renewal_terms("pro", "price_plan", "month", None)
    assert "$249.00 USD" in text
    assert [key for key, _ctx in alerts] == ["price_catalog_mismatch:price_plan"]


def test_a_price_on_the_wrong_interval_for_its_slot_pages_ops(stripe_prices):
    # The annual slot pointing at a monthly Price at the annual amount: amount
    # matches, but the pricing page and the configured interval are wrong.
    prices, alerts = stripe_prices
    plan = get_plan("pro")
    prices.update(price_annual=_stripe_price(plan["price_annual"] * 100, interval="month"))
    billing._checkout_renewal_terms("pro", "price_annual", "year", None)
    assert alerts and alerts[0][1]["configured_interval"] == "year"


def test_a_stripe_read_failure_fails_the_checkout_closed(monkeypatch):
    def _down(pid, **kw):
        raise billing.stripe.error.APIConnectionError("connection reset")

    monkeypatch.setattr(billing.stripe.Price, "retrieve", _down)
    with pytest.raises(HTTPException) as exc:
        billing._checkout_renewal_terms("pro", "price_plan", "month", None)
    assert exc.value.status_code == 503


@pytest.mark.parametrize(("plan_price", "metered_price"), [
    (_stripe_price(None), None),                                  # no flat amount
    (_stripe_price(19900, scheme="tiered"), None),
    ({"unit_amount": 19900, "currency": "usd", "billing_scheme": "per_unit"}, None),  # one-off
    (_stripe_price(19900, usage="metered"), None),                # a metered plan Price
    (_stripe_price(19900), _stripe_price(8, usage="licensed")),   # a licensed "overage"
    (_stripe_price(19900), _stripe_price(8, usage="metered", scheme="tiered")),
])
def test_a_price_of_the_wrong_kind_is_refused(stripe_prices, plan_price, metered_price):
    prices, _alerts = stripe_prices
    prices.update(price_plan=plan_price, price_metered=metered_price)
    with pytest.raises(HTTPException) as exc:
        billing._checkout_renewal_terms("pro", "price_plan", "month",
                                        "price_metered" if metered_price else None)
    assert exc.value.status_code == 503
