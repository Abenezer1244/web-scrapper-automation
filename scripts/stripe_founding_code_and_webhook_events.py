"""Make FOUNDING25 enterable, and let the billing webhook hear recovered payments.

Two independent Stripe fixes, both found live on 2026-09-13:

  1. The FOUNDING25 coupon exists (25% off, forever, all products, 25 spots) and
     /billing/plans advertises it, but no Promotion Code was ever created on it.
     Checkout only accepts promotion CODES, so nobody could redeem it. This
     creates the code FOUNDING25 on that coupon: no customer restriction, no
     product restriction (monthly and annual both), no code-level redemption
     cap or expiry, because the coupon's own max_redemptions=25 is the one limit.

  2. The live webhook endpoint never subscribed to invoice.payment_succeeded, so
     _handle_payment_succeeded (billing.py) never ran and a customer who fixed a
     failed card stayed in dunning. This ADDS that event to the endpoint's
     existing list; every event already enabled is kept.

Each fix checks what is already there first and reports rather than overwrites
anything that differs from what was asked. Nothing is changed without --apply,
and a live key is refused without --live.

Usage (STRIPE_SECRET_KEY from the environment, e.g. via `railway run`):

    python scripts/stripe_founding_code_and_webhook_events.py --webhook we_...
    python scripts/stripe_founding_code_and_webhook_events.py --webhook we_... --apply --live
"""
import argparse
import os
import sys

import stripe

FOUNDING_COUPON_ID = "FOUNDING25"
FOUNDING_CODE = "FOUNDING25"
REQUIRED_EVENT = "invoice.payment_succeeded"


def fail(message: str) -> int:
    print(f"ERROR: {message}")
    return 1


def _coupon_id_of(promo) -> str | None:
    # Older API versions embed the coupon; newer ones nest it under `promotion`.
    coupon = promo.get("coupon") or (promo.get("promotion") or {}).get("coupon")
    return coupon if isinstance(coupon, str) or coupon is None else coupon.get("id")


def ensure_founding_code(apply: bool) -> int:
    coupon = stripe.Coupon.retrieve(FOUNDING_COUPON_ID, expand=["applies_to"])
    mismatches = [
        label for label, ok in (
            ("valid", coupon.get("valid") is True),
            ("percent_off", coupon.get("percent_off") == 25),
            ("duration", coupon.get("duration") == "forever"),
            ("max_redemptions", coupon.get("max_redemptions") == 25),
            # FOUNDING25 must reach every plan, monthly and annual.
            ("applies_to", not (coupon.get("applies_to") or {}).get("products")),
        ) if not ok
    ]
    if mismatches:
        return fail(
            f"coupon {FOUNDING_COUPON_ID} is not the founding offer the app advertises "
            f"(differs in: {', '.join(mismatches)}); not creating a code on it"
        )
    print(f"  coupon {FOUNDING_COUPON_ID}: 25% forever, all products, "
          f"{coupon.get('times_redeemed') or 0}/25 redeemed")

    # Any code already spelled FOUNDING25 (active or not), and any code at all on
    # this coupon, is reported instead of silently duplicated.
    same_code = list(stripe.PromotionCode.list(code=FOUNDING_CODE, limit=100).auto_paging_iter())
    on_coupon = list(
        stripe.PromotionCode.list(coupon=FOUNDING_COUPON_ID, limit=100).auto_paging_iter()
    )
    for promo in {p["id"]: p for p in same_code + on_coupon}.values():
        print(f"  existing promotion code {promo['id']} code={promo.get('code')!r} "
              f"active={promo.get('active')} coupon={_coupon_id_of(promo)!r} "
              f"customer={promo.get('customer')!r} expires_at={promo.get('expires_at')!r}")

    active_same = [p for p in same_code if p.get("active")]
    if active_same:
        promo = active_same[0]
        if _coupon_id_of(promo) != FOUNDING_COUPON_ID or promo.get("customer") \
                or promo.get("expires_at") or promo.get("max_redemptions"):
            return fail(
                f"active code {promo['id']} is spelled {FOUNDING_CODE} but is not the open "
                "founding code (other coupon, customer, expiry or cap); archive it first"
            )
        print(f"  EXISTS  {promo['id']} is already the open {FOUNDING_CODE} code; nothing created")
        return 0

    if not apply:
        print(f"  WOULD CREATE promotion code {FOUNDING_CODE} on coupon {FOUNDING_COUPON_ID} "
              "(no customer, no expiry, no code-level cap)")
        return 0

    promo = stripe.PromotionCode.create(
        coupon=FOUNDING_COUPON_ID,
        code=FOUNDING_CODE,
        metadata={"bridgeleads_resource": "founding_offer"},
        idempotency_key=f"founding_offer:promotion_code:{FOUNDING_COUPON_ID}:{FOUNDING_CODE}",
    )
    stored = stripe.PromotionCode.retrieve(promo["id"])
    if not (stored.get("active") and stored.get("code") == FOUNDING_CODE
            and _coupon_id_of(stored) == FOUNDING_COUPON_ID and not stored.get("customer")):
        return fail(f"promotion code {promo['id']} did not read back as the open founding code")
    print(f"  CREATED promotion code {promo['id']} ({FOUNDING_CODE}, active)")
    return 0


def ensure_webhook_event(webhook_id: str, apply: bool) -> int:
    endpoint = stripe.WebhookEndpoint.retrieve(webhook_id)
    events = list(endpoint.get("enabled_events") or [])
    print(f"  webhook {webhook_id} status={endpoint.get('status')} "
          f"api_version={endpoint.get('api_version')} events={sorted(events)}")
    if endpoint.get("status") != "enabled":
        return fail(f"webhook {webhook_id} is not enabled; not modifying it")
    if "*" in events or REQUIRED_EVENT in events:
        print(f"  EXISTS  {REQUIRED_EVENT} is already delivered; nothing changed")
        return 0

    wanted = events + [REQUIRED_EVENT]
    if not apply:
        print(f"  WOULD ADD {REQUIRED_EVENT} (keeping all {len(events)} existing events)")
        return 0

    stripe.WebhookEndpoint.modify(webhook_id, enabled_events=wanted)
    stored = set(stripe.WebhookEndpoint.retrieve(webhook_id).get("enabled_events") or [])
    if stored != set(wanted):
        return fail(f"webhook {webhook_id} read back as {sorted(stored)}, expected {sorted(wanted)}")
    print(f"  ADDED   {REQUIRED_EVENT}; events now {sorted(stored)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--webhook",
        help="webhook endpoint id (we_...); omit to leave the webhook untouched",
    )
    parser.add_argument("--apply", action="store_true", help="make the changes (default: report only)")
    parser.add_argument("--live", action="store_true", help="required with a live key")
    args = parser.parse_args()

    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        return fail("STRIPE_SECRET_KEY is not set in the environment")
    is_live = key.startswith(("sk_live_", "rk_live_"))
    if is_live and not args.live:
        return fail("this is a LIVE key; pass --live to use it")
    stripe.api_key = key

    print(f"mode={'APPLY' if args.apply else 'DRY RUN'} stripe={'LIVE' if is_live else 'TEST'}")
    print("FOUNDING25 promotion code:")
    code_rc = ensure_founding_code(args.apply)
    if not args.webhook:
        print(f"webhook {REQUIRED_EVENT}: skipped (no --webhook)")
        return code_rc
    print(f"webhook {REQUIRED_EVENT}:")
    webhook_rc = ensure_webhook_event(args.webhook, args.apply)
    return code_rc or webhook_rc


if __name__ == "__main__":
    sys.exit(main())
