"""Create a single-customer promotion: N months of a paid plan at 100% off.

Everything is Stripe's own machinery, so Stripe stays the authority on whether
the code is valid when it is typed into Checkout:

  * a Coupon: percent_off=100, duration=repeating, duration_in_months=N,
    max_redemptions=1, applies_to = the plan's PRODUCT only (so the metered
    skip-trace overage, a separate product, is still billed), redeem_by;
  * a Promotion Code on it: restricted to ONE Stripe customer,
    max_redemptions=1, expires_at.

Nothing about the recipient is stored in application code. The plan price, and
therefore the entitlement, is unchanged: a 100%-off Agency subscription is an
Agency subscription (see tests/test_promo_access.py).

Monthly only, enforced by Stripe. On an annual price Stripe applies a repeating
coupon to the whole yearly invoice, so "3 months free" would be a free year.
Annual prices therefore live on their own Products, the coupon applies only to
the monthly product, and this script REFUSES to issue it against a product that
carries any yearly price (active or archived). Stripe then declines the discount
on annual lines everywhere: Checkout, plan switches and the Dashboard. General
codes such as FOUNDING25 are untouched.

Refuses to run when the customer already has a live subscription: Checkout will
refuse them too (one subscription per customer), and the right tool for an
existing MONTHLY subscriber is attaching the coupon to that subscription in the
Dashboard, never a second subscription.

Usage (reads STRIPE_SECRET_KEY and STRIPE_PRICE_<PLAN> from the environment,
NOT from a .env in the repo):

    python scripts/stripe_single_customer_promo.py --plan agency \\
        --customer cus_... --user-id <bridgeleads user uuid> --code <CODE> --dry-run
    (no Stripe customer yet: replace --customer with --email <exact account email>)
    python scripts/stripe_single_customer_promo.py ... (same args, no --dry-run)

A live key is refused unless --live is passed.
"""
import argparse
import os
import sys
import time

import stripe

TAG_KEY = "bridgeleads_resource"
TAG_VALUE = "single_customer_promo"
PAID_PLANS = ("pro", "business", "agency")
TERMINAL_SUBSCRIPTION_STATUSES = ("canceled", "incomplete_expired")


def fail(message: str) -> int:
    print(f"ERROR: {message}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True, choices=PAID_PLANS)
    parser.add_argument("--customer", help="Stripe customer id (cus_...), if the recipient has one")
    parser.add_argument(
        "--email",
        help="the account's EXACT BridgeLeads email; used to adopt or create the customer "
             "when --customer is not given",
    )
    parser.add_argument(
        "--user-id", required=True,
        help="BridgeLeads user id; must equal the customer's metadata.user_id",
    )
    parser.add_argument("--code", required=True, help="the code the customer will type")
    parser.add_argument("--months", type=int, default=3)
    parser.add_argument(
        "--redeem-days", type=int, default=14,
        help="days the code can be redeemed; the discount itself then runs --months",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true", help="required with a live key")
    args = parser.parse_args()

    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key:
        return fail("STRIPE_SECRET_KEY is not set in the environment")
    is_live = key.startswith(("sk_live_", "rk_live_"))
    if is_live and not args.live:
        return fail("this is a LIVE key; pass --live once test mode has been verified")
    if not 1 <= args.months <= 11:
        return fail("--months must be 1-11 (a year or more is not a promotion of this kind)")
    if not 1 <= args.redeem_days <= 90:
        return fail("--redeem-days must be 1-90")
    stripe.api_key = key

    price_env = f"STRIPE_PRICE_{args.plan.upper()}"
    price_id = os.environ.get(price_env, "")
    if not price_id.startswith("price_"):
        return fail(f"{price_env} must be set to the plan's MONTHLY price id")

    print(f"mode={'DRY RUN' if args.dry_run else 'CREATE'} "
          f"stripe={'LIVE' if is_live else 'TEST'} plan={args.plan}")

    price = stripe.Price.retrieve(price_id)
    recurring = price.get("recurring") or {}
    if not price.get("active") or recurring.get("interval") != "month":
        return fail(f"{price_env}={price_id} is not an active monthly price")
    product_id = price["product"]
    print(f"  price   {price_id} ({price.get('unit_amount')} {price.get('currency')}/month)")
    print(f"  product {product_id} (coupon applies to this product only)")

    # The invariant everything rests on: the product this coupon may discount
    # sells no yearly price. Archived prices count, because an existing annual
    # subscription can still be on one.
    yearly = [
        p["id"] for p in stripe.Price.list(product=product_id, limit=100).auto_paging_iter()
        if (p.get("recurring") or {}).get("interval") == "year"
    ]
    if yearly:
        return fail(
            f"product {product_id} also carries yearly price(s) {', '.join(yearly)}. "
            "A 3-month coupon on it would make a whole annual invoice free. Move the "
            "annual prices to their own product first."
        )
    print(f"  product {product_id} has no yearly prices (coupon cannot reach an annual invoice)")

    if not args.customer:
        # The recipient has never been to Checkout, so there is no Stripe customer
        # to restrict the code to. Adopt or create one exactly the way checkout
        # does (src/api/routes/billing.py _resolve_existing_customer): same email,
        # metadata.user_id = their BridgeLeads id. Their checkout then resolves to
        # THIS customer, which is the one the code is restricted to.
        if not args.email:
            return fail("pass --customer, or --email (the account's exact email) to adopt/create one")
        # By OWNER, over every customer. Customer.list(email=) is case-sensitive and
        # Customer.search lags new customers by up to a minute (a sandbox re-run
        # with different email casing created a duplicate through both), so the
        # only lookup that cannot miss this user's customer is a full scan on
        # metadata.user_id. Fine for an ops script on this account's volume.
        live = [
            c for c in stripe.Customer.list(limit=100).auto_paging_iter()
            if (c.get("metadata") or {}).get("user_id") == args.user_id and not c.get("deleted")
        ]
        if len(live) > 1:
            return fail(
                f"user {args.user_id} already owns {len(live)} Stripe customers "
                f"({', '.join(c['id'] for c in live)}); reconcile them before issuing a code"
            )
        if live and live[0].get("email") != args.email:
            return fail(
                f"customer {live[0]['id']} for this user has email {live[0].get('email')!r}, "
                f"not {args.email!r}. Checkout adopts by the account's exact email, so the "
                "code would be restricted to a customer checkout never uses."
            )
        args.customer = live[0]["id"] if live else None
        if args.customer:
            print(f"  customer {args.customer} adopted (metadata.user_id + exact email match)")
        elif args.dry_run:
            print(f"  WOULD CREATE customer for {args.email} with metadata.user_id={args.user_id}, "
                  "then the coupon and a code restricted to it")
            return 0
        else:
            args.customer = stripe.Customer.create(
                email=args.email, metadata={"user_id": args.user_id},
                idempotency_key=f"{TAG_VALUE}:customer:{args.user_id}:{args.email}",
            )["id"]
            print(f"  CREATED customer {args.customer} for user {args.user_id}")

    customer = stripe.Customer.retrieve(args.customer)
    if customer.get("deleted"):
        return fail(f"customer {args.customer} is deleted")
    owner = (customer.get("metadata") or {}).get("user_id")
    if owner != args.user_id:
        return fail(
            f"customer {args.customer} belongs to user_id={owner!r}, not {args.user_id!r}; "
            "checkout only adopts a customer whose metadata.user_id matches the account"
        )
    print(f"  customer {args.customer} (metadata.user_id matches)")

    for sub in stripe.Subscription.list(
        customer=args.customer, status="all", limit=100
    ).auto_paging_iter():
        if sub.get("status") not in TERMINAL_SUBSCRIPTION_STATUSES:
            return fail(
                f"customer already has subscription {sub['id']} ({sub['status']}). "
                "Checkout will refuse a second one. For a MONTHLY subscriber, attach "
                "the coupon to that subscription in the Dashboard instead."
            )

    existing = stripe.PromotionCode.list(
        code=args.code, customer=args.customer, active=True, limit=1
    ).get("data") or []
    if existing:
        # "Already there" is only success if it is the promotion that was asked
        # for. A same-spelled code on another coupon would otherwise be reported
        # as done while it grants a different product or term.
        promo = existing[0]
        # Older API versions embed the coupon; newer ones nest it, possibly by id.
        # Retrieved again either way, EXPANDED: Stripe omits `applies_to` from
        # every coupon response unless asked for it, and the product
        # restriction is the one field that must not be taken on trust.
        coupon = promo.get("coupon") or (promo.get("promotion") or {}).get("coupon") or {}
        coupon_id = coupon if isinstance(coupon, str) else coupon.get("id")
        coupon = stripe.Coupon.retrieve(coupon_id, expand=["applies_to"]) if coupon_id else {}
        requested_end = int(time.time()) + args.redeem_days * 86400
        window_slack = 86400  # a re-run on a later day still asks for the same window
        mismatches = [
            label for label, ok in (
                ("percent_off", coupon.get("percent_off") == 100),
                ("duration", coupon.get("duration") == "repeating"),
                ("duration_in_months", coupon.get("duration_in_months") == args.months),
                ("applies_to", (coupon.get("applies_to") or {}).get("products") == [product_id]),
                ("coupon max_redemptions", coupon.get("max_redemptions") == 1),
                ("code max_redemptions", promo.get("max_redemptions") == 1),
                ("customer", promo.get("customer") == args.customer),
                ("expires_at", promo.get("expires_at") is not None
                 and abs(promo["expires_at"] - requested_end) <= window_slack),
                ("coupon redeem_by", coupon.get("redeem_by") is not None
                 and abs(coupon["redeem_by"] - requested_end) <= window_slack),
            ) if not ok
        ]
        if mismatches:
            return fail(
                f"an active promotion code {promo['id']} with this code already exists "
                f"for the customer but differs in: {', '.join(mismatches)}. Archive it "
                "in the Dashboard before creating the one you asked for."
            )
        print(f"  EXISTS  promotion code {promo['id']} already matches this promotion; "
              "nothing created")
        return 0

    redeem_by = int(time.time()) + args.redeem_days * 86400
    metadata = {TAG_KEY: TAG_VALUE, "plan": args.plan, "user_id": args.user_id}
    if args.dry_run:
        print(f"  WOULD CREATE coupon: 100% off, repeating {args.months} month(s), "
              f"max_redemptions=1, applies_to={product_id}, redeem_by={redeem_by}")
        print(f"  WOULD CREATE promotion code for {args.customer}: max_redemptions=1, "
              f"expires_at={redeem_by}")
        return 0

    # Idempotency keys make a retried run (a timeout after Stripe accepted the
    # request) return the same objects instead of a second coupon.
    # Every material parameter is in the key, so a retry after a configuration
    # change (another product, term or window) cannot be handed the old coupon.
    idem = (
        f"{TAG_VALUE}:{args.customer}:{args.code.upper()}:{args.plan}:{product_id}:"
        f"{args.months}:{args.redeem_days}"
    )
    coupon = stripe.Coupon.create(
        percent_off=100,
        duration="repeating",
        duration_in_months=args.months,
        max_redemptions=1,
        applies_to={"products": [product_id]},
        redeem_by=redeem_by,
        name=f"{args.plan.title()}: {args.months} months free",
        metadata=metadata,
        idempotency_key=f"{idem}:coupon",
    )
    print(f"  CREATED coupon {coupon['id']}")
    # Read the restriction back rather than trusting the request: a coupon that
    # silently applied to every product would reach annual invoices.
    stored = stripe.Coupon.retrieve(coupon["id"], expand=["applies_to"])
    if (stored.get("applies_to") or {}).get("products") != [product_id]:
        stripe.Coupon.delete(coupon["id"])
        return fail(
            f"coupon {coupon['id']} did not keep applies_to=[{product_id}]; it was "
            "deleted and no promotion code was created"
        )
    promo = stripe.PromotionCode.create(
        coupon=coupon["id"],
        code=args.code,
        customer=args.customer,
        max_redemptions=1,
        expires_at=redeem_by,
        metadata=metadata,
        idempotency_key=f"{idem}:promotion_code",
    )
    print(f"  CREATED promotion code {promo['id']} (customer-restricted, expires {redeem_by})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
