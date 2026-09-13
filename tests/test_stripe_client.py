"""Every Stripe call is bounded by our timeout and retry budget, not the SDK's."""
import stripe

from src.config import settings
from src.config.stripe_client import configure_stripe


def test_the_stripe_sdk_uses_the_configured_timeout_and_retries(monkeypatch):
    monkeypatch.setattr(stripe, "default_http_client", None)
    monkeypatch.setattr(stripe, "max_network_retries", 2)
    monkeypatch.setattr(settings, "STRIPE_TIMEOUT_SECONDS", 7)
    monkeypatch.setattr(settings, "STRIPE_MAX_NETWORK_RETRIES", 1)

    configure_stripe()

    assert stripe.default_http_client._timeout == 7
    assert stripe.max_network_retries == 1
    assert stripe.api_key == settings.STRIPE_SECRET_KEY


def test_configuring_twice_keeps_one_client_and_a_changed_timeout_replaces_it(monkeypatch):
    monkeypatch.setattr(stripe, "default_http_client", None)
    monkeypatch.setattr(settings, "STRIPE_TIMEOUT_SECONDS", 7)
    configure_stripe()
    first = stripe.default_http_client

    configure_stripe()
    assert stripe.default_http_client is first

    monkeypatch.setattr(settings, "STRIPE_TIMEOUT_SECONDS", 12)
    configure_stripe()
    assert stripe.default_http_client is not first
    assert stripe.default_http_client._timeout == 12


def test_the_billing_routes_configure_stripe_when_imported():
    from src.api.routes import billing  # noqa: F401 - the import is the subject

    assert stripe.default_http_client is not None
    assert stripe.default_http_client._timeout == settings.STRIPE_TIMEOUT_SECONDS
