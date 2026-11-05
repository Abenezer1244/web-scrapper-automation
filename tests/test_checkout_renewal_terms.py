"""The automatic-renewal disclosure shown on Stripe Checkout (ROSCA).

Compliance audit 2026-11, Phase 5a. Pure: the wording is built from the plan
catalog and lookup rates Stripe bills from, so these tests pin it to them.
"""

import pytest

from src.api.routes.billing import _renewal_terms
from src.config import settings
from src.config.lookup_pricing import unit_price_cents
from src.config.plans import get_plan

_PAID = ("pro", "business", "agency")


@pytest.mark.parametrize("plan_id", _PAID)
@pytest.mark.parametrize(("interval", "word", "price_key"),
                         [("month", "month", "price_monthly"), ("year", "year", "price_annual")])
def test_states_the_recurring_charge_from_the_catalog(plan_id, interval, word, price_key):
    plan = get_plan(plan_id)
    text = _renewal_terms(plan_id, interval, metered=True)
    assert f"renews automatically every {word} for ${plan[price_key]:,} USD" in text
    assert "until you cancel or the subscription otherwise ends" in text
    assert "To cancel, open Billing in your BridgeLeads settings." in text


@pytest.mark.parametrize("plan_id", _PAID)
def test_states_the_allowance_and_the_plans_own_overage_rate(plan_id):
    text = _renewal_terms(plan_id, "month", metered=True)
    assert f"{settings.SKIP_TRACE_BUNDLED_QUOTAS[plan_id]:,} contact lookups each month" in text
    assert f"${unit_price_cents(plan_id) / 100:.2f} USD each" in text


def test_agency_overage_is_its_own_rate_not_pros():
    assert "$0.05 USD each" in _renewal_terms("agency", "month", metered=True)
    assert "$0.08" not in _renewal_terms("agency", "month", metered=True)


def test_no_overage_rate_is_quoted_when_no_metered_item_is_on_the_session():
    # An unprovisioned interval sells the plan unmetered: quoting a charge that
    # cannot happen would misstate the terms.
    text = _renewal_terms("pro", "year", metered=False)
    assert "additional lookups" not in text
    assert "contact lookups each month" in text


@pytest.mark.parametrize("plan_id", _PAID)
@pytest.mark.parametrize("interval", ["month", "year"])
def test_fits_stripe_limits_and_house_style(plan_id, interval):
    text = _renewal_terms(plan_id, interval, metered=True)
    assert len(text) <= 1200          # Stripe custom_text maximum
    assert "—" not in text       # no em dashes in user-facing copy
    # Claims the code cannot vouch for stay out (Codex consult).
    for unvouched in ("anytime", "end of the", "refund", "less any promotion"):
        assert unvouched not in text.lower(), unvouched
