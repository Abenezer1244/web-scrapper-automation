"""Authoritative plan catalog: price, record limit, and marketed features.

Single source of truth for what a plan COSTS and what it INCLUDES. Extracted out
of ``src/api/routes/billing.py`` so non-API callers (Celery workers building
transactional email copy) can read the same numbers the billing endpoints and
the pricing page serve, instead of keeping their own copy.

Before this module existed the trial-expiry email hardcoded "Pro ($79/mo)" while
billing had moved Pro to $199 — a duplicated constant that silently drifted and
quoted a price we do not charge. Anything that shows a user a price or a record
limit MUST read it from here.

This module deliberately holds no Stripe/HTTP/session imports so a worker can
import it without pulling FastAPI in.
"""

from typing import Any

from src.config import settings

# Ordered cheapest → most expensive; the API serves this list verbatim.
#
# "features" bullets describe ENFORCED entitlements only. Per-tier county /
# record-type gating is the value-metric build (separate phase); until it ships
# we do NOT advertise a county cap the backend does not honor.
PLAN_CATALOG: list[dict[str, Any]] = [
    {
        "id": "starter",
        "name": "Starter",
        "price_monthly": 0,
        "records_limit": 50,
        "features": ["50 records/month", "1 county", "CSV export", "Manual runs"],
        "stripe_price_id": None,
    },
    {
        "id": "pro",
        "name": "Pro",
        "price_monthly": 199,
        "price_annual": 1910,  # ~$159/mo, ~20% off
        "records_limit": 1000,
        # Bullets describe ENFORCED entitlements only (value-metric build,
        # docs/pricing-strategy-2026-06.md §4): Pro = 3 counties + the 4 core
        # distress lists (incl. Auction Leads). Premium lists + overlap are a
        # Business feature.
        "features": [
            "1,000 records/month",
            "3 counties (your choice)",
            "Probate, pre-foreclosure, tax-delinquent & auction lists",
            "Skip tracing (250 included, then $0.08/lookup)",
            "CSV + Excel export",
            "Daily/weekly schedule",
            "Email delivery",
            "Batch scraping",
        ],
        "stripe_price_id": settings.STRIPE_PRICE_PRO,
        "stripe_price_id_annual": settings.STRIPE_PRICE_PRO_ANNUAL,
        "popular": True,
    },
    {
        "id": "business",
        "name": "Business",
        "price_monthly": 499,
        "price_annual": 4790,  # ~$399/mo, ~20% off
        "records_limit": 5000,
        "features": [
            "5,000 records/month",
            "10 counties (your choice)",
            "All record types + overlap/intersection",
            "All export formats",
            "All schedules",
            "Email + Webhook + dialer delivery",
            "Skip tracing (1,000 included)",
            "API access",
        ],
        "stripe_price_id": settings.STRIPE_PRICE_BUSINESS,
        "stripe_price_id_annual": settings.STRIPE_PRICE_BUSINESS_ANNUAL,
    },
    {
        "id": "agency",
        "name": "Agency",
        "price_monthly": 1499,
        "price_annual": 14390,  # ~$1,199/mo, ~20% off
        "records_limit": -1,
        "features": [
            "Unlimited counties + records",
            "All record types + overlap/intersection",
            "Skip tracing (2,000 included)",
            "White-label (coming soon)",
            "Priority queue + support",
            "Dedicated account manager",
        ],
        "stripe_price_id": settings.STRIPE_PRICE_AGENCY,
        "stripe_price_id_annual": settings.STRIPE_PRICE_AGENCY_ANNUAL,
    },
]
_BY_ID: dict[str, dict[str, Any]] = {p["id"]: p for p in PLAN_CATALOG}


def get_plan(plan_id: str) -> dict[str, Any]:
    """Return the catalog entry for ``plan_id``.

    Raises KeyError for an unknown plan rather than returning a default — a
    caller rendering a price must never silently quote the wrong tier.
    """
    return _BY_ID[plan_id]


def plan_price_monthly(plan_id: str) -> int:
    """Monthly list price in whole USD (0 for a free plan)."""
    return int(get_plan(plan_id)["price_monthly"])


def plan_records_limit(plan_id: str) -> int:
    """Monthly record allowance (-1 = unlimited)."""
    return int(get_plan(plan_id)["records_limit"])


def format_price_monthly(plan_id: str) -> str:
    """Human price for email/UI copy, e.g. ``$199/month``. Free plans read 'Free'."""
    price = plan_price_monthly(plan_id)
    return "Free" if price <= 0 else f"${price:,}/month"


def format_records_limit(plan_id: str) -> str:
    """Human record allowance for copy, e.g. ``1,000 records per month``."""
    limit = plan_records_limit(plan_id)
    if limit < 0:
        return "Unlimited records"
    return f"{limit:,} records per month"
