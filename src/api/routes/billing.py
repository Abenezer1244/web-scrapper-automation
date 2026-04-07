"""Stripe billing routes: checkout, portal, webhooks, plans, usage."""

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import CurrentUser
from src.config import settings
from src.db import User, get_db
from src.api.deps import get_rls_db
from src.utils.logger import setup_logger

_logger = setup_logger("billing")

stripe.api_key = settings.STRIPE_SECRET_KEY

router = APIRouter(prefix="/billing", tags=["billing"])

# ─── Plan catalog ─────────────────────────────────────────────────────────────

_PLANS = [
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
        "price_monthly": 99,
        "records_limit": 500,
        "features": ["500 records/month", "5 counties", "CSV + Excel export", "Daily schedule", "Email delivery"],
        "stripe_price_id": settings.STRIPE_PRICE_PRO,
    },
    {
        "id": "business",
        "name": "Business",
        "price_monthly": 299,
        "records_limit": 5000,
        "features": [
            "5,000 records/month",
            "Unlimited counties",
            "All export formats",
            "All schedules",
            "Email + Webhook delivery",
            "Skip tracing",
            "API access",
        ],
        "stripe_price_id": settings.STRIPE_PRICE_BUSINESS,
    },
    {
        "id": "agency",
        "name": "Agency",
        "price_monthly": 799,
        "records_limit": -1,
        "features": [
            "Unlimited records",
            "Unlimited counties",
            "All features",
            "Team members",
            "White-label",
            "Priority support",
        ],
        "stripe_price_id": settings.STRIPE_PRICE_AGENCY,
    },
]

# price_id → (plan_name, records_limit)
_PRICE_TO_PLAN: dict[str, tuple[str, int]] = {
    p["stripe_price_id"]: (p["id"], p["records_limit"])
    for p in _PLANS
    if p["stripe_price_id"]
}


# ─── Plans catalog ────────────────────────────────────────────────────────────

@router.get("/plans")
async def list_plans() -> list[dict]:
    """Return the full plan catalog. Used by the frontend upgrade UI."""
    return _PLANS



# ─── Usage ────────────────────────────────────────────────────────────────────

@router.get("/usage")
async def get_usage(current_user: CurrentUser) -> dict:
    """Return current plan, record usage, and limit for the settings page."""
    limit = current_user.records_limit
    used = current_user.records_used
    return {
        "plan": current_user.plan,
        "records_used": used,
        "records_limit": limit,
        "records_remaining": max(0, limit - used) if limit != -1 else None,
        "percent_used": round((used / limit) * 100, 1) if limit and limit != -1 else 0,
    }


# ─── Subscription status ──────────────────────────────────────────────────────

@router.get("/subscription")
async def get_subscription(current_user: CurrentUser) -> dict:
    """Return the user's active Stripe subscription details, if any."""
    if not current_user.stripe_customer_id:
        return {"status": "none", "plan": current_user.plan}

    try:
        subscriptions = stripe.Subscription.list(
            customer=current_user.stripe_customer_id,
            status="active",
            limit=1,
            expand=["data.items.data.price"],
        )
        if not subscriptions.data:
            return {"status": "none", "plan": current_user.plan}

        sub = subscriptions.data[0]
        price = sub["items"]["data"][0]["price"]
        return {
            "status": sub["status"],
            "plan": current_user.plan,
            "current_period_end": sub["current_period_end"],
            "cancel_at_period_end": sub["cancel_at_period_end"],
            "price_id": price["id"],
            "amount_monthly": price["unit_amount"] // 100,
            "currency": price["currency"],
        }
    except stripe.error.StripeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not retrieve subscription: {exc.user_message}",
        )


# ─── Checkout ─────────────────────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    price_id: str


@router.post("/checkout")
async def create_checkout(
    body: CheckoutRequest,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> dict:
    """Create a Stripe Checkout session to upgrade the user's plan."""
    price_or_product_id = body.price_id

    # Validate: input must be a known product/price ID from the plan catalog
    if price_or_product_id not in _PRICE_TO_PLAN:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid plan")

    # Resolve Product ID → Price ID (hardcoded to avoid extra Stripe API calls)
    _PRODUCT_TO_PRICE = {
        "prod_UANuoAMKafnDJ5": "price_1TC38PHE9wT1C7yZ7XDpF2Ln",  # Pro
        "prod_UANwwzFn0msFok": "price_1TC3AgHE9wT1C7yZWVcdX3cv",  # Business
        "prod_UANxJNomPNWE5l": "price_1TC3BRHE9wT1C7yZ6Jja7hHZ",  # Agency
    }
    stripe_price_id = _PRODUCT_TO_PRICE.get(price_or_product_id, price_or_product_id)

    try:
        customer_id = current_user.stripe_customer_id
        if not customer_id:
            customer = stripe.Customer.create(
                email=current_user.email,
                metadata={"user_id": current_user.id},
            )
            customer_id = customer.id
            result = await db.execute(select(User).where(User.id == current_user.id))
            user = result.scalar_one()
            user.stripe_customer_id = customer_id
            await db.flush()

        session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            payment_method_types=["card"],
            line_items=[{"price": stripe_price_id, "quantity": 1}],
            success_url=f"{settings.FRONTEND_URL}/settings?upgrade=success",
            cancel_url=f"{settings.FRONTEND_URL}/settings?upgrade=cancelled",
            metadata={"user_id": current_user.id, "price_id": price_or_product_id},
            allow_promotion_codes=True,
        )
        return {"checkout_url": session.url}
    except HTTPException:
        raise
    except Exception as e:
        _logger.exception("Checkout failed for user %s", current_user.id)
        raise HTTPException(status_code=502, detail="Checkout temporarily unavailable")


# ─── Customer portal ──────────────────────────────────────────────────────────

@router.post("/portal")
async def customer_portal(current_user: CurrentUser) -> dict:
    """Return a Stripe Customer Portal URL for managing subscriptions."""
    if not current_user.stripe_customer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No active subscription. Choose a plan below to get started.",
        )
    session = stripe.billing_portal.Session.create(
        customer=current_user.stripe_customer_id,
        return_url=f"{settings.FRONTEND_URL}/settings",
    )
    return {"portal_url": session.url}


# ─── Webhook ──────────────────────────────────────────────────────────────────

@router.post("/webhook", status_code=status.HTTP_200_OK)
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    stripe_signature: str = Header(..., alias="stripe-signature"),
) -> dict:
    """Handle Stripe webhook events to keep plan state in sync.

    Registers for:
      - checkout.session.completed      → activate new plan
      - customer.subscription.updated   → handle upgrades / downgrades
      - customer.subscription.deleted   → downgrade to starter
      - invoice.payment_failed          → notify user by email
    """
    if not settings.STRIPE_WEBHOOK_SECRET or len(settings.STRIPE_WEBHOOK_SECRET) < 20:
        raise HTTPException(status_code=503, detail="Webhook not configured")

    payload = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload, stripe_signature, settings.STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature",
        )

    # Idempotency: skip duplicate webhook deliveries
    import redis.asyncio as aioredis
    redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    event_id = event.get("id", "")
    if event_id:
        dedup_key = f"stripe_event:{event_id}"
        if await redis.get(dedup_key):
            await redis.aclose()
            return {"received": True}
        await redis.setex(dedup_key, 3600, "1")
    await redis.aclose()

    event_type: str = event["type"]
    data: dict = event["data"]["object"]

    if event_type == "checkout.session.completed":
        await _handle_checkout_completed(data, db)

    elif event_type == "customer.subscription.updated":
        await _handle_subscription_updated(data, db)

    elif event_type == "customer.subscription.deleted":
        await _handle_subscription_deleted(data, db)

    elif event_type == "invoice.payment_failed":
        await _handle_payment_failed(data, db)

    return {"received": True}


# ─── Webhook handlers ─────────────────────────────────────────────────────────

async def _handle_checkout_completed(data: dict, db: AsyncSession) -> None:
    """Activate new plan after a successful checkout session."""
    user_id = (data.get("metadata") or {}).get("user_id")
    subscription_id = data.get("subscription")

    if not user_id or not subscription_id:
        return

    subscription = stripe.Subscription.retrieve(
        subscription_id,
        expand=["items.data.price"],
    )
    price_id = subscription["items"]["data"][0]["price"]["id"]
    plan_info = _PRICE_TO_PLAN.get(price_id)

    if not plan_info:
        return

    plan_name, records_limit = plan_info
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user:
        user.plan = plan_name
        user.records_limit = records_limit
        await db.flush()


async def _handle_subscription_updated(data: dict, db: AsyncSession) -> None:
    """Handle plan changes (upgrades or downgrades) mid-cycle."""
    customer_id = data.get("customer")
    if not customer_id:
        return

    items = (data.get("items") or {}).get("data", [])
    if not items:
        return

    price_id = items[0]["price"]["id"]
    plan_info = _PRICE_TO_PLAN.get(price_id)

    if not plan_info:
        return

    plan_name, records_limit = plan_info
    result = await db.execute(select(User).where(User.stripe_customer_id == customer_id))
    user = result.scalar_one_or_none()
    if user:
        user.plan = plan_name
        user.records_limit = records_limit
        await db.flush()


async def _handle_subscription_deleted(data: dict, db: AsyncSession) -> None:
    """Downgrade user to starter when their subscription ends."""
    customer_id = data.get("customer")
    if not customer_id:
        return

    result = await db.execute(select(User).where(User.stripe_customer_id == customer_id))
    user = result.scalar_one_or_none()
    if user:
        user.plan = "starter"
        user.records_limit = settings.PLAN_LIMITS["starter"]
        await db.flush()


async def _handle_payment_failed(data: dict, db: AsyncSession) -> None:
    """Send a payment failure notification email via Resend."""
    customer_id = data.get("customer")
    attempt_count = data.get("attempt_count", 1)

    if not customer_id:
        return

    result = await db.execute(select(User).where(User.stripe_customer_id == customer_id))
    user = result.scalar_one_or_none()
    if not user:
        return

    # Send notification — imported here to avoid circular at startup
    from src.workers.delivery import _send_payment_failed_email
    _send_payment_failed_email(user.email, attempt_count)
