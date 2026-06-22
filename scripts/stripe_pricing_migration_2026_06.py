#!/usr/bin/env python3
"""Stripe pricing migration to the $199 strategy (docs/pricing-strategy-2026-06.md).

Creates the new monthly + annual Price objects (Pro/Business/Agency) under the
EXISTING products, plus the FOUNDING25 coupon. Stripe Prices are immutable, so
we create new ones and archive the old; products are reused.

Source of truth for the resulting price ids is the printed output + the doc
written by --emit-doc. After creating, set these on Railway (api AND worker):
  STRIPE_PRICE_PRO / _BUSINESS / _AGENCY            (new monthly price_ ids)
  STRIPE_PRICE_PRO_ANNUAL / _BUSINESS_ / _AGENCY_   (new annual  price_ ids)

Usage:
  STRIPE_SECRET_KEY=sk_live_... python scripts/stripe_pricing_migration_2026_06.py verify
  STRIPE_SECRET_KEY=sk_live_... python scripts/stripe_pricing_migration_2026_06.py create [--commit]

`create` without --commit is a DRY RUN (prints what it would do, no writes).
It is idempotent: it skips a price if an active price with the same product +
amount + interval already exists, and skips the coupon if FOUNDING25 exists.
"""
from __future__ import annotations

import argparse
import os
import sys

import stripe

# Existing products (currently — wrongly — stored in STRIPE_PRICE_* envs).
PRODUCTS = {
    "pro": "prod_UANuoAMKafnDJ5",
    "business": "prod_UANwwzFn0msFok",
    "agency": "prod_UANxJNomPNWE5l",
}

# amount in whole dollars → cents. Annual = ~20% off monthly*12, rounded.
PRICING = {
    "pro": {"monthly": 199, "annual": 1910},
    "business": {"monthly": 499, "annual": 4790},
    "agency": {"monthly": 1499, "annual": 14390},
}

FOUNDING_COUPON_NAME = "FOUNDING25"
FOUNDING_PERCENT_OFF = 25
FOUNDING_MAX_REDEMPTIONS = 25


def _find_existing_price(product_id: str, amount_cents: int, interval: str):
    """Return an active price matching product+amount+interval, or None."""
    prices = stripe.Price.list(product=product_id, active=True, limit=100)
    for p in prices.auto_paging_iter():
        rec = p.get("recurring") or {}
        if (
            p.get("unit_amount") == amount_cents
            and p.get("currency") == "usd"
            and rec.get("interval") == interval
        ):
            return p
    return None


def verify() -> None:
    for plan, pid in PRODUCTS.items():
        try:
            prod = stripe.Product.retrieve(pid)
        except stripe.error.StripeError as exc:  # noqa: PERF203
            print(f"  {plan:9} {pid}  ERROR: {exc.user_message or exc}")
            continue
        print(f"  {plan:9} {pid}  name={prod.get('name')!r} active={prod.get('active')}")
        prices = stripe.Price.list(product=pid, active=True, limit=100)
        for p in prices.auto_paging_iter():
            rec = p.get("recurring") or {}
            amt = (p.get("unit_amount") or 0) / 100
            print(
                f"      price {p['id']}  ${amt:,.0f} {p.get('currency')}"
                f" /{rec.get('interval','one-time')}  active={p.get('active')}"
            )
    try:
        c = stripe.Coupon.retrieve(FOUNDING_COUPON_NAME)
        print(
            f"  coupon {FOUNDING_COUPON_NAME}: {c.get('percent_off')}% off, valid={c.get('valid')},"
            f" redeemed={c.get('times_redeemed')}/{c.get('max_redemptions')}"
        )
    except stripe.error.StripeError:
        print(f"  coupon {FOUNDING_COUPON_NAME}: (not found)")


def create(commit: bool) -> None:
    tag = "CREATE" if commit else "DRY-RUN"
    result: dict[str, str] = {}
    for plan, pid in PRODUCTS.items():
        for interval, key in (("month", "monthly"), ("year", "annual")):
            amount_cents = PRICING[plan][key] * 100
            existing = _find_existing_price(pid, amount_cents, interval)
            env = (
                f"STRIPE_PRICE_{plan.upper()}"
                + ("" if interval == "month" else "_ANNUAL")
            )
            if existing:
                print(f"[{tag}] {env}: reuse existing {existing['id']} (${amount_cents/100:,.0f}/{interval})")
                result[env] = existing["id"]
                continue
            if not commit:
                print(f"[{tag}] {env}: WOULD create ${amount_cents/100:,.0f}/{interval} under {pid}")
                continue
            price = stripe.Price.create(
                product=pid,
                unit_amount=amount_cents,
                currency="usd",
                recurring={"interval": interval},
                nickname=f"{plan} {interval}ly (2026-06 strategy)",
            )
            print(f"[{tag}] {env}: created {price['id']} (${amount_cents/100:,.0f}/{interval})")
            result[env] = price["id"]

    # Founding coupon
    try:
        stripe.Coupon.retrieve(FOUNDING_COUPON_NAME)
        print(f"[{tag}] coupon {FOUNDING_COUPON_NAME}: already exists, skipping")
    except stripe.error.StripeError:
        if commit:
            c = stripe.Coupon.create(
                id=FOUNDING_COUPON_NAME,
                percent_off=FOUNDING_PERCENT_OFF,
                duration="forever",
                max_redemptions=FOUNDING_MAX_REDEMPTIONS,
                name="Founding member (25% off)",
            )
            print(f"[{tag}] coupon: created {c['id']} ({FOUNDING_PERCENT_OFF}% off)")
        else:
            print(f"[{tag}] coupon {FOUNDING_COUPON_NAME}: WOULD create {FOUNDING_PERCENT_OFF}% off, max {FOUNDING_MAX_REDEMPTIONS}")

    if result:
        print("\n--- Railway env (set on BOTH api and worker) ---")
        for k, v in result.items():
            print(f"{k}={v}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["verify", "create"])
    ap.add_argument("--commit", action="store_true", help="actually write to Stripe (create mode)")
    args = ap.parse_args()

    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key.startswith("sk_"):
        print("ERROR: STRIPE_SECRET_KEY missing or invalid", file=sys.stderr)
        return 2
    stripe.api_key = key

    if args.mode == "verify":
        verify()
    else:
        create(commit=args.commit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
