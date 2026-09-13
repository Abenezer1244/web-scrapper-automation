"""One place that points the Stripe SDK at our key, timeout and retry budget.

stripe-python keeps its HTTP client, key and retry count as module globals, so
this runs once per process: when the API imports its billing routes and when
the Celery app is built. Left alone the SDK waits 80 seconds per attempt and
retries twice, and the billing webhooks make their Stripe calls while holding a
user's row lock, so one slow Stripe response could hold that lock for minutes.

Idempotent: re-running with the same settings keeps the existing client.
"""
import stripe

from src.config import settings


def configure_stripe() -> None:
    stripe.api_key = settings.STRIPE_SECRET_KEY
    stripe.max_network_retries = settings.STRIPE_MAX_NETWORK_RETRIES
    current = stripe.default_http_client
    if current is None or getattr(current, "_timeout", None) != settings.STRIPE_TIMEOUT_SECONDS:
        stripe.default_http_client = stripe.new_default_http_client(
            timeout=settings.STRIPE_TIMEOUT_SECONDS
        )
