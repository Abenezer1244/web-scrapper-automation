"""Stripe billing routes: checkout, portal, webhooks, plans, usage."""

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import CurrentUser
from src.api.middleware import rate_limit
from src.config import settings
from src.db import User, get_db
from src.api.deps import get_rls_db
from src.utils.logger import setup_logger

_logger = setup_logger("billing")

stripe.api_key = settings.STRIPE_SECRET_KEY

router = APIRouter(prefix="/billing", tags=["billing"])


@router.get("/activation-funnel")
async def activation_funnel(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    days: int = 30,
) -> dict:
    """Sprint 5.5: activation funnel metrics (admin-only).

    Returns the conversion funnel across the last `days` days:
      signup -> first scraper -> first job -> first download -> paid upgrade

    All derived from existing tables — no new schema needed.
    Percentages are computed from signup count, so every step shows both
    an absolute count and a conversion rate from signup.
    """
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )

    if days < 1 or days > 365:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="days must be between 1 and 365",
        )

    from sqlalchemy import text as sa_text

    # Use raw SQL for the funnel — expresses the intent more clearly
    # than 5 separate SQLAlchemy expressions.
    result = await db.execute(
        sa_text("""
            WITH window_users AS (
                SELECT id, plan, created_at
                FROM users
                WHERE created_at >= NOW() - (:days || ' days')::interval
                  AND is_active = true
            )
            SELECT
                -- 1. Signups in window
                (SELECT COUNT(*) FROM window_users) AS signups,

                -- 2. Users who created at least one scraper_config
                (SELECT COUNT(DISTINCT u.id)
                 FROM window_users u
                 JOIN scraper_configs sc ON sc.user_id = u.id) AS first_scraper,

                -- 3. Users who ran at least one scrape job (any status)
                (SELECT COUNT(DISTINCT u.id)
                 FROM window_users u
                 JOIN jobs j ON j.user_id = u.id) AS first_job,

                -- 4. Users whose job produced a downloadable export
                (SELECT COUNT(DISTINCT u.id)
                 FROM window_users u
                 JOIN jobs j ON j.user_id = u.id
                 WHERE j.export_key IS NOT NULL) AS first_download,

                -- 5. Users who upgraded to a paid plan (any of pro/business/agency)
                (SELECT COUNT(*)
                 FROM window_users
                 WHERE LOWER(plan) IN ('pro', 'business', 'agency')
                   AND stripe_customer_id IS NOT NULL) AS paid_upgrade
        """),
        {"days": str(days)},
    )
    row = result.fetchone()
    if row is None:
        return {"days": days, "signups": 0, "funnel": []}

    signups = row.signups or 0
    first_scraper = row.first_scraper or 0
    first_job = row.first_job or 0
    first_download = row.first_download or 0
    paid_upgrade = row.paid_upgrade or 0

    def _pct(n: int) -> float:
        return round(100 * n / signups, 1) if signups else 0.0

    return {
        "days": days,
        "signups": signups,
        "funnel": [
            {"step": "signup", "count": signups, "pct_from_signup": 100.0},
            {"step": "first_scraper", "count": first_scraper, "pct_from_signup": _pct(first_scraper)},
            {"step": "first_job", "count": first_job, "pct_from_signup": _pct(first_job)},
            {"step": "first_download", "count": first_download, "pct_from_signup": _pct(first_download)},
            {"step": "paid_upgrade", "count": paid_upgrade, "pct_from_signup": _pct(paid_upgrade)},
        ],
        # Step-to-step conversion rates (what the dropoff looks like)
        "step_conversions": {
            "signup_to_scraper": _pct(first_scraper),
            "scraper_to_job": round(100 * first_job / first_scraper, 1) if first_scraper else 0.0,
            "job_to_download": round(100 * first_download / first_job, 1) if first_job else 0.0,
            "download_to_paid": round(100 * paid_upgrade / first_download, 1) if first_download else 0.0,
        },
    }


@router.get("/referral")
async def referral_status(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Sprint 7.3: referral program — code, stats, and credit balance.

    Returns:
      - code: the user's shareable referral code
      - share_url: canonical signup URL with ?ref= appended
      - referred_count: number of users who signed up via this code
      - paid_conversions: number of those users who converted to paid
      - credit_earned_cents / credit_earned_usd: running balance
      - bonus_per_conversion_cents: display constant for the frontend

    Referrals that don't yet have a code (legacy accounts created
    before migration 017) get one generated on first call so the
    endpoint is always safe to hit.
    """
    from sqlalchemy import func as sa_func

    from src.db.models import ReferralEvent

    # Ensure the current user has a referral code — backfill if null
    # for legacy accounts.
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one()
    if not user.referral_code:
        import secrets
        _ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
        for _ in range(8):
            candidate = "".join(secrets.choice(_ALPHABET) for _ in range(8))
            existing = await db.execute(
                select(User).where(User.referral_code == candidate)
            )
            if existing.scalar_one_or_none() is None:
                user.referral_code = candidate
                await db.flush()
                break

    # How many users signed up via this code?
    referred_res = await db.execute(
        select(sa_func.count(User.id)).where(User.referred_by_user_id == user.id)
    )
    referred_count = referred_res.scalar() or 0

    # How many of those triggered a bonus (i.e. converted to paid)?
    paid_res = await db.execute(
        select(sa_func.count(ReferralEvent.id)).where(
            ReferralEvent.referrer_id == user.id
        )
    )
    paid_conversions = paid_res.scalar() or 0

    base = settings.PUBLIC_APP_URL.rstrip("/") if hasattr(settings, "PUBLIC_APP_URL") else "https://app.bridgeleads.io"
    share_url = f"{base}/signup?ref={user.referral_code}"

    return {
        "code": user.referral_code,
        "share_url": share_url,
        "referred_count": int(referred_count),
        "paid_conversions": int(paid_conversions),
        "credit_earned_cents": user.referral_credit_cents or 0,
        "credit_earned_usd": round((user.referral_credit_cents or 0) / 100, 2),
        "bonus_per_conversion_cents": _REFERRAL_BONUS_CENTS,
    }


@router.get("/skip-trace-usage")
async def skip_trace_usage(
    current_user: CurrentUser,
) -> dict:
    """Return the user's skip-trace lookup usage + bundled quota.

    Used by the frontend billing page to render a progress bar and
    overage estimate. Values are read from the cached counter on the
    User row — no external calls.
    """
    plan = (current_user.plan or "starter").lower()
    quota = settings.SKIP_TRACE_BUNDLED_QUOTAS.get(plan, 0)
    used = current_user.skip_trace_used_this_month or 0
    overage_units = max(0, used - quota)

    # Per-lookup overage rate by plan (see PRD v1.3 §5.4)
    overage_rate_usd: float | None
    if plan == "agency":
        overage_rate_usd = 0.05
    elif plan in ("pro", "business"):
        overage_rate_usd = 0.08
    else:
        overage_rate_usd = None

    estimated_charges_usd = round(overage_units * (overage_rate_usd or 0), 2)

    return {
        "plan": plan,
        "quota": quota,
        "used": used,
        "remaining": max(0, quota - used) if quota > 0 else None,
        "overage_units": overage_units,
        "overage_rate_usd": overage_rate_usd,
        "estimated_charges_usd": estimated_charges_usd,
        "period_start": (
            current_user.skip_trace_period_start.isoformat()
            if current_user.skip_trace_period_start
            else None
        ),
    }

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
        "price_monthly": 49,
        "records_limit": 500,
        "features": [
            "500 records/month",
            "5 counties",
            "All record types",
            "CSV + Excel export",
            "Daily/weekly schedule",
            "Email delivery",
        ],
        "stripe_price_id": settings.STRIPE_PRICE_PRO,
    },
    {
        "id": "business",
        "name": "Business",
        "price_monthly": 149,
        "records_limit": 5000,
        "features": [
            "5,000 records/month",
            "Unlimited counties",
            "All export formats",
            "All schedules",
            "Email + Webhook delivery",
            "Skip tracing (coming soon)",
            "API access",
            "5 team members",
        ],
        "stripe_price_id": settings.STRIPE_PRICE_BUSINESS,
    },
    {
        "id": "agency",
        "name": "Agency",
        "price_monthly": 499,
        "records_limit": -1,
        "features": [
            "Unlimited records",
            "Unlimited counties",
            "All features",
            "Unlimited team members",
            "White-label (coming soon)",
            "Priority support",
            "Dedicated account manager",
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


@router.get("/pricing")
async def pricing_page() -> dict:
    """Return full pricing page data including feature comparison matrix.

    Public endpoint — no auth required. Used by the frontend pricing page.
    """
    return {
        "plans": _PLANS,
        "comparison": {
            "Records per month": {"starter": "50", "pro": "500", "business": "5,000", "agency": "Unlimited"},
            "Counties": {"starter": "1", "pro": "5", "business": "Unlimited", "agency": "Unlimited"},
            "Record types": {"starter": "Probate", "pro": "All", "business": "All", "agency": "All"},
            "Data freshness": {"starter": "Daily", "pro": "Daily", "business": "Daily", "agency": "Daily"},
            "Export formats": {"starter": "CSV", "pro": "CSV, Excel", "business": "CSV, Excel, JSON, API", "agency": "CSV, Excel, JSON, API"},
            "Scheduling": {"starter": "Manual only", "pro": "Daily, Weekly", "business": "All frequencies", "agency": "All frequencies"},
            "Email delivery": {"starter": False, "pro": True, "business": True, "agency": True},
            "Webhook delivery": {"starter": False, "pro": False, "business": True, "agency": True},
            "Skip tracing": {"starter": False, "pro": False, "business": "Coming soon", "agency": "Coming soon"},
            "API access": {"starter": False, "pro": False, "business": True, "agency": True},
            "Team members": {"starter": "1", "pro": "1", "business": "5", "agency": "Unlimited"},
            "White-label": {"starter": False, "pro": False, "business": False, "agency": "Coming soon"},
            "Support": {"starter": "Community", "pro": "Email", "business": "Priority email", "agency": "Dedicated manager"},
        },
        "trial": {
            "days": 7,
            "plan": "pro",
            "description": "7-day free Pro trial. No credit card required. 500 records/month.",
        },
        "faq": [
            {"q": "What are motivated seller leads?", "a": "Public records (probate, foreclosure, tax delinquent, etc.) that indicate a property owner may be willing to sell below market value."},
            {"q": "How fresh is the data?", "a": "We scrape county portals daily. Records appear in BridgeLeads within 24 hours of being filed at the county."},
            {"q": "What counties do you cover?", "a": "Currently all 39 Washington State counties, with expansion to TX, FL, CA, and more states coming soon."},
            {"q": "Can I cancel anytime?", "a": "Yes. No contracts, no cancellation fees. Your data exports remain available for 30 days after cancellation."},
            {"q": "What export formats do you support?", "a": "CSV, Excel, and JSON. Business and Agency plans also get API access for direct integration."},
        ],
    }


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

    # Resolve Product ID → Price ID first (sourced from env vars via settings)
    _PRODUCT_TO_PRICE = {
        settings.STRIPE_PRODUCT_PRO: settings.STRIPE_PRICE_PRO,
        settings.STRIPE_PRODUCT_BUSINESS: settings.STRIPE_PRICE_BUSINESS,
        settings.STRIPE_PRODUCT_AGENCY: settings.STRIPE_PRICE_AGENCY,
    }
    stripe_price_id = _PRODUCT_TO_PRICE.get(price_or_product_id, price_or_product_id)

    # Validate: resolved price must be a known plan price
    if stripe_price_id not in _PRICE_TO_PLAN:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid plan")

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

    # C5 (full-SaaS review): rate-limit BEFORE the HMAC check so that
    # a flood of invalid-signature requests can't burn CPU. Stripe
    # sends legitimate webhooks at well under the 120/min cap; an
    # attacker spraying bogus events gets 429'd quickly.
    await rate_limit(request, zone="webhook")

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

    # Idempotency: skip duplicate webhook deliveries. Uses SET NX EX
    # (atomic set-if-not-exists with TTL) so two Stripe retries
    # delivered within milliseconds of each other cannot both pass
    # the check — the second call returns None from .set() and we
    # bail. The prior get-then-setex pattern was racy and allowed
    # duplicate plan updates + duplicate notification emails when
    # Stripe's retry latency overlapped request processing. C4 from
    # the full-SaaS review. Stripe retries for up to 3 days, so we
    # keep the TTL at 3 days to cover the full retry window.
    import redis.asyncio as aioredis
    redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        event_id = event.get("id", "")
        if event_id:
            dedup_key = f"stripe_event:{event_id}"
            # set(..., nx=True, ex=N) returns True on first write,
            # None on conflict. On conflict we've already processed
            # this event — return success so Stripe stops retrying.
            claimed = await redis.set(dedup_key, "1", nx=True, ex=259200)  # 3 days
            if not claimed:
                _logger.info("stripe webhook dedup: already processed %s", event_id)
                return {"received": True}
    finally:
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

        # Sprint 7.3: grant referral credit if this is the referee's
        # first paid conversion. The unique constraint on
        # referral_events.referee_id makes this idempotent against
        # webhook replay.
        if user.referred_by_user_id:
            await _grant_referral_credit(db, user)


_REFERRAL_BONUS_CENTS = 2000  # $20 per successful referral


async def _grant_referral_credit(db: AsyncSession, referee: User) -> None:
    """Credit the referrer $20 when a referred user converts to paid.

    Idempotent: the unique constraint on referral_events.referee_id
    guarantees each referee can only trigger the bonus once, even if
    Stripe replays the webhook.
    """
    from sqlalchemy.exc import IntegrityError

    from src.db.models import ReferralEvent

    # Fetch the referrer (they may no longer exist if account was
    # deleted — SET NULL would have already cleared the FK).
    referrer_res = await db.execute(
        select(User).where(User.id == referee.referred_by_user_id)
    )
    referrer = referrer_res.scalar_one_or_none()
    if referrer is None:
        return  # Referrer was deleted between signup and conversion

    # Create the audit-log row inside a SAVEPOINT so that a duplicate
    # (webhook replay hitting the unique constraint on referee_id)
    # only rolls back the INSERT, not the enclosing transaction.
    # Without the savepoint, `db.rollback()` after the IntegrityError
    # would throw away the plan upgrade and records_limit change that
    # _handle_checkout_completed just flushed — silently reverting a
    # paid user to their old plan on every Stripe retry. CRIT-1 from
    # the Sprint 7.3 code review.
    event = ReferralEvent(
        referrer_id=referrer.id,
        referee_id=referee.id,
        amount_cents=_REFERRAL_BONUS_CENTS,
        reason="referee_converted_to_paid",
    )
    try:
        async with db.begin_nested():
            db.add(event)
            # flush is implicit at savepoint exit, but being explicit
            # makes the IntegrityError surface here rather than later
            await db.flush()
    except IntegrityError:
        _logger.info(
            "referral: duplicate grant ignored referee=%s", referee.id
        )
        return

    # Atomic increment on the referrer's balance so concurrent
    # grants from different referees don't race. Kept outside the
    # savepoint — if this UPDATE fails, the whole webhook transaction
    # should roll back (caller will see the exception and Stripe will
    # retry) because we absolutely cannot have an audit-log row
    # without a matching balance increment.
    from sqlalchemy import update as sa_update
    await db.execute(
        sa_update(User)
        .where(User.id == referrer.id)
        .values(referral_credit_cents=User.referral_credit_cents + _REFERRAL_BONUS_CENTS)
    )
    await db.flush()
    _logger.info(
        "referral: +$20 credit referrer=%s referee=%s", referrer.id, referee.id
    )


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
