"""The Stripe webhook handlers used to silently `return` on a price that isn't in
_PRICE_TO_PLAN — a PAID user's entitlement was then never activated, with no log
or alert. These tests prove the gap is now surfaced loudly instead of dropped.

No mocks (per testing rules): send_ops_alert is a real no-op when OPS_ALERT_EMAIL
is unset (the test env), and _alert_billing_gap is defensively exception-safe
regardless, so calling these never sends a real email and never raises.
"""
import logging

import pytest

from src.api.routes.billing import _alert_billing_gap, _handle_subscription_updated


def test_alert_billing_gap_logs_error_and_never_raises(caplog):
    with caplog.at_level(logging.ERROR):
        # Must not raise even if alerting is unconfigured or fails downstream.
        _alert_billing_gap(
            "test gap",
            "unmapped-price:price_TEST",
            price_id="price_TEST",
            user_id="u1",
        )
    assert any(
        "billing webhook gap" in r.getMessage() for r in caplog.records
    ), "expected a loud ERROR log for the billing gap"


@pytest.mark.asyncio
async def test_subscription_updated_unmapped_price_is_surfaced(caplog):
    # The unmapped-price branch returns BEFORE touching the DB, so db is unused on
    # this path — passing None exercises exactly the early-return we hardened.
    event = {
        "customer": "cus_TEST",
        "items": {"data": [{"price": {"id": "price_NOT_IN_MAP"}}]},
    }
    with caplog.at_level(logging.ERROR):
        await _handle_subscription_updated(event, db=None)  # db unused on this path
    assert any(
        "price not in plan map" in r.getMessage() for r in caplog.records
    ), "an unmapped subscription price must be logged loudly, not silently dropped"
