"""Provision the YEARLY metered skip-trace Prices, idempotently.

Why these have to exist separately from the monthly ones: Stripe requires every
item in a subscription to share a recurring interval, so the monthly metered
price cannot ride on an annual plan subscription. Without a yearly twin,
``_metered_skip_trace_price()`` (src/api/routes/billing.py) attaches nothing to
an annual checkout and every over-quota lookup on an annual plan is recorded,
metered and free. That is deliberate in the code: selling the plan matters more
than metering its overage, so the missing price logs a warning instead of
failing the sale. This script is how the warning goes away.

Same meter, same product, same per-unit amounts as the monthly prices
(pro 8c, business 8c, agency 5c). Only ``recurring.interval`` differs.

The included allowance is still counted per ENTITLEMENT WINDOW, which is one
month even for an annual subscriber, so an annual Pro customer still gets 250
included lookups a month. What changes is only when Stripe invoices the excess:
the meter sums the reported units across the subscription's billing period, so
an annual subscriber's overage lands on their annual invoice rather than a
monthly one.

Idempotent: finds by metadata tag first and creates nothing that already exists.
Prices cannot be deleted once created, only deactivated, so a re-run must never
duplicate.

Usage (reads STRIPE_SECRET_KEY from the environment, NOT from a .env in the repo):
    python scripts/stripe_setup_skip_trace_annual.py --dry-run
    python scripts/stripe_setup_skip_trace_annual.py

Then set on the api AND worker Railway services:
    STRIPE_PRICE_SKIP_TRACE_PRO_ANNUAL
    STRIPE_PRICE_SKIP_TRACE_BUSINESS_ANNUAL
    STRIPE_PRICE_SKIP_TRACE_AGENCY_ANNUAL
"""
import argparse
import os
import sys

import stripe

TAG_KEY = "bridgeleads_resource"
TAG_VALUE = "sprint4_skip_trace"
INTERVAL_TAG = "annual"

# (tier, unit_amount_cents, env var). Amounts mirror the monthly prices exactly:
# an annual subscriber must not be quoted a different per-lookup rate than the
# pricing page shows.
TIERS = [
    ("pro", 8, "STRIPE_PRICE_SKIP_TRACE_PRO_ANNUAL"),
    ("business", 8, "STRIPE_PRICE_SKIP_TRACE_BUSINESS_ANNUAL"),
    ("agency", 5, "STRIPE_PRICE_SKIP_TRACE_AGENCY_ANNUAL"),
]


def find_existing(product_id: str, meter_id: str, tier: str):
    """An annual metered price for this tier, or None. Matched on the METADATA
    tag plus the shape, never on amount alone: a price with the right amount and
    the wrong interval is exactly the bug this script exists to avoid."""
    for price in stripe.Price.list(product=product_id, active=True, limit=100).auto_paging_iter():
        md = price.get("metadata") or {}
        rec = price.get("recurring") or {}
        if (
            md.get(TAG_KEY) == TAG_VALUE
            and md.get("tier") == tier
            and md.get("interval_tag") == INTERVAL_TAG
            and rec.get("interval") == "year"
            and rec.get("usage_type") == "metered"
            and rec.get("meter") == meter_id
        ):
            return price
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        print("ERROR: STRIPE_SECRET_KEY is not set in the environment")
        return 1
    stripe.api_key = key
    product_id = os.environ.get("STRIPE_PRODUCT_SKIP_TRACE", "")
    meter_id = os.environ.get("STRIPE_METER_SKIP_TRACE", "")
    if not product_id or not meter_id:
        print("ERROR: STRIPE_PRODUCT_SKIP_TRACE and STRIPE_METER_SKIP_TRACE must be set")
        return 1

    print(f"mode={'DRY RUN' if args.dry_run else 'LIVE'} key={key[:7]}...")
    print(f"product={product_id} meter={meter_id}")

    env_lines = []
    for tier, cents, env_var in TIERS:
        existing = find_existing(product_id, meter_id, tier)
        if existing:
            print(f"  {tier:9} EXISTS  {existing['id']} ({existing['unit_amount']}c/yr metered)")
            env_lines.append(f"{env_var}={existing['id']}")
            continue
        if args.dry_run:
            print(f"  {tier:9} WOULD CREATE  {cents}c per unit, interval=year, metered")
            continue
        price = stripe.Price.create(
            product=product_id,
            currency="usd",
            unit_amount=cents,
            billing_scheme="per_unit",
            recurring={
                "interval": "year",
                "interval_count": 1,
                "usage_type": "metered",
                "meter": meter_id,
            },
            nickname=f"Skip Trace Lookup ({tier}, annual)",
            metadata={TAG_KEY: TAG_VALUE, "tier": tier, "interval_tag": INTERVAL_TAG},
        )
        print(f"  {tier:9} CREATED {price['id']}")
        env_lines.append(f"{env_var}={price['id']}")

    if env_lines:
        print("\nSet these on the api AND worker services:")
        for line in env_lines:
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
