"""Idempotently provision Stripe resources for Sprint 4 skip-trace metered billing.

Creates (if missing):
  1. Product "Skip Trace Lookup"
  2. Meter "skip_trace_lookup" (event_name=skip_trace_lookup, aggregated by sum)
  3. Three metered Prices attached to the meter:
       - Pro tier:      $0.08/unit
       - Business tier: $0.08/unit (overage after 1000 bundled)
       - Agency tier:   $0.05/unit (overage after 2000 bundled)

All operations check existing resources first via metadata tags so this
script is safe to run multiple times without creating duplicates.

Writes discovered/created IDs to .env as:
  STRIPE_PRODUCT_SKIP_TRACE
  STRIPE_METER_SKIP_TRACE
  STRIPE_PRICE_SKIP_TRACE_PRO
  STRIPE_PRICE_SKIP_TRACE_BUSINESS
  STRIPE_PRICE_SKIP_TRACE_AGENCY

⚠️  Uses STRIPE_SECRET_KEY from .env — this is a LIVE key. All resources
created here are real and billable. Resources can be deactivated via
Stripe dashboard if needed, but cannot be fully deleted once referenced
by a subscription.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from dotenv import load_dotenv

load_dotenv(".env")

import stripe

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
if not stripe.api_key:
    print("ERROR: STRIPE_SECRET_KEY missing")
    raise SystemExit(1)

# Tag all resources with this metadata key so we can find them on re-run
SPRINT_TAG_KEY = "bridgeleads_resource"
SPRINT_TAG_VALUE = "sprint4_skip_trace"

PRODUCT_NAME = "Skip Trace Lookup"
METER_EVENT_NAME = "skip_trace_lookup"
METER_DISPLAY_NAME = "Skip Trace Lookups"


def find_product() -> stripe.Product | None:
    """Find existing product by metadata tag (idempotency)."""
    print(f"Searching for existing product with metadata {SPRINT_TAG_KEY}={SPRINT_TAG_VALUE}...")
    # Stripe's list with search is better than paginating manually
    try:
        results = stripe.Product.search(
            query=f'metadata["{SPRINT_TAG_KEY}"]:"{SPRINT_TAG_VALUE}" AND active:"true"',
            limit=5,
        )
        for p in results.auto_paging_iter():
            if p.name == PRODUCT_NAME:
                return p
    except Exception as exc:
        print(f"  search failed: {exc}")
    return None


def find_meter() -> stripe.billing.Meter | None:
    """Find existing meter by event_name."""
    print(f"Searching for existing meter with event_name={METER_EVENT_NAME}...")
    meters = stripe.billing.Meter.list(limit=100)
    for m in meters.auto_paging_iter():
        if m.event_name == METER_EVENT_NAME:
            return m
    return None


def find_price_by_metadata(product_id: str, tier: str) -> stripe.Price | None:
    """Find existing metered price for a specific tier."""
    prices = stripe.Price.list(product=product_id, limit=100, active=True)
    for p in prices.auto_paging_iter():
        if p.metadata.get("tier") == tier:
            return p
    return None


def write_env_vars(values: dict) -> None:
    """Upsert keys into .env without printing any secrets."""
    env_path = ".env"
    with open(env_path, "r", encoding="utf-8") as f:
        content = f.read()
    for key, value in values.items():
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        if pattern.search(content):
            content = pattern.sub(f"{key}={value}", content)
        else:
            if not content.endswith("\n"):
                content += "\n"
            content += f"{key}={value}\n"
    with open(env_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def main() -> int:
    print("=== Stripe skip-trace metered billing setup ===\n")
    print(f"Using key: {stripe.api_key[:12]}... (mode: {'LIVE' if stripe.api_key.startswith('sk_live') else 'TEST'})")
    print()

    # 1) Product
    product = find_product()
    if product:
        print(f"[product] EXISTING: {product.id} name={product.name}")
    else:
        print(f"[product] creating new...")
        product = stripe.Product.create(
            name=PRODUCT_NAME,
            description="Per-lookup phone + email append via Tracerfy. Billed monthly on top of base subscription.",
            metadata={
                SPRINT_TAG_KEY: SPRINT_TAG_VALUE,
                "sprint": "4",
                "provider": "tracerfy",
            },
        )
        print(f"[product] CREATED: {product.id}")

    # 2) Meter
    meter = find_meter()
    if meter:
        print(f"[meter] EXISTING: {meter.id} event={meter.event_name}")
    else:
        print(f"[meter] creating new...")
        meter = stripe.billing.Meter.create(
            display_name=METER_DISPLAY_NAME,
            event_name=METER_EVENT_NAME,
            default_aggregation={"formula": "sum"},
            customer_mapping={
                "type": "by_id",
                "event_payload_key": "stripe_customer_id",
            },
            value_settings={"event_payload_key": "value"},
        )
        print(f"[meter] CREATED: {meter.id}")

    # 3) Three metered Prices
    # Pricing per PRD §5.4: Pro $0.08, Business overage $0.08, Agency overage $0.05
    # All billed monthly in USD, billing scheme = per_unit, tiers not used
    # (simple flat-per-unit rates; bundled quotas are enforced in our
    #  backend by only reporting events beyond the bundle).
    price_configs = [
        ("pro", 8),                 # $0.08 -> 8 cents
        ("business_overage", 8),    # $0.08 -> 8 cents
        ("agency_overage", 5),      # $0.05 -> 5 cents
    ]
    price_ids: dict[str, str] = {}
    for tier, unit_amount in price_configs:
        existing = find_price_by_metadata(product.id, tier)
        if existing:
            print(f"[price/{tier}] EXISTING: {existing.id} unit=${existing.unit_amount/100:.2f}")
            price_ids[tier] = existing.id
            continue

        print(f"[price/{tier}] creating ${unit_amount/100:.2f}/unit metered...")
        price = stripe.Price.create(
            product=product.id,
            currency="usd",
            unit_amount=unit_amount,
            recurring={
                "interval": "month",
                "usage_type": "metered",
                "meter": meter.id,
            },
            metadata={
                "tier": tier,
                SPRINT_TAG_KEY: SPRINT_TAG_VALUE,
            },
        )
        price_ids[tier] = price.id
        print(f"[price/{tier}] CREATED: {price.id}")

    # 4) Write IDs to .env
    env_values = {
        "STRIPE_PRODUCT_SKIP_TRACE": product.id,
        "STRIPE_METER_SKIP_TRACE": meter.id,
        "STRIPE_METER_EVENT_NAME_SKIP_TRACE": METER_EVENT_NAME,
        "STRIPE_PRICE_SKIP_TRACE_PRO": price_ids["pro"],
        "STRIPE_PRICE_SKIP_TRACE_BUSINESS_OVERAGE": price_ids["business_overage"],
        "STRIPE_PRICE_SKIP_TRACE_AGENCY_OVERAGE": price_ids["agency_overage"],
    }
    write_env_vars(env_values)
    print()
    print("=== .env updated with: ===")
    for k, v in env_values.items():
        print(f"  {k}={v}")

    print()
    print("=== Summary ===")
    print(f"  Product:   {product.id}")
    print(f"  Meter:     {meter.id} (event={METER_EVENT_NAME})")
    print(f"  Pro:       {price_ids['pro']}")
    print(f"  Business:  {price_ids['business_overage']} (overage)")
    print(f"  Agency:    {price_ids['agency_overage']} (overage)")
    print()
    print("Next: set these Price IDs on Railway env vars, then the webhook")
    print("ingest code will report usage via stripe.billing.MeterEvent.create.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
