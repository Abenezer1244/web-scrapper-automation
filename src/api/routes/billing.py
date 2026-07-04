"""Stripe billing routes: checkout, portal, webhooks, plans, usage."""

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import CurrentUser, require_admin
from src.api.deps import get_rls_db
from src.api.middleware import client_ip, rate_limit
from src.config import settings
from src.db import User, get_db
from src.utils.logger import setup_logger

_logger = setup_logger("billing")

stripe.api_key = settings.STRIPE_SECRET_KEY

router = APIRouter(prefix="/billing", tags=["billing"])


async def _rate_limit_activation_funnel(request: Request) -> None:
    """IP-keyed limiter that runs BEFORE require_admin (Codex P2).

    The admin gate is a route dependency, so it would reject a non-admin caller
    before any in-body limiter — leaving denied funnel probes unthrottled and
    each one still paying an auth decode (Redis + DB user lookup). Rate-limiting
    here, ahead of the gate, throttles ALL callers (admin and non-admin) before
    the gate or the raw-SQL funnel runs. IP-keyed because non-admins are rejected
    before we'd trust any per-user identity, and the funnel is admin-only +
    low-traffic so an IP bucket is appropriate.
    """
    await rate_limit(
        request, zone="general", identifier=f"admin-funnel:{client_ip(request)}"
    )


@router.get(
    "/activation-funnel",
    dependencies=[Depends(_rate_limit_activation_funnel), Depends(require_admin)],
)
async def activation_funnel(
    db: AsyncSession = Depends(get_db),
    days: int = 30,
) -> dict:
    """Sprint 5.5: activation funnel metrics (admin-only).

    Returns the conversion funnel across the last `days` days:
      signup -> first scraper -> first job -> first download -> paid upgrade

    All derived from existing tables — no new schema needed.
    Percentages are computed from signup count, so every step shows both
    an absolute count and a conversion rate from signup.

    Access (H2-P5): require_admin gates this route — non-admins get 404 (endpoint
    hidden) and admins who have not enrolled MFA get 403
    admin_mfa_enrollment_required. This is a READ-ONLY analytics surface, so it
    requires MFA *enrollment* but not a fresh step-up; the state-changing admin
    op (connector creation) is the one that uses require_admin_mfa.

    Rate-limiting + admin gating both run as route dependencies before this body
    (_rate_limit_activation_funnel then require_admin), so the raw-SQL funnel is
    reached only by a throttled, authenticated admin.
    """
    if days < 1 or days > 365:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="days must be between 1 and 365",
        )

    # Cross-tenant aggregate via the SECURITY DEFINER public.activation_funnel()
    # (migration 029). Under the NOBYPASSRLS bridgeleads_app role this admin
    # route cannot read across all users directly; the definer function (owned
    # by a privileged role, EXECUTE granted only to bridgeleads_app) returns
    # ONLY the funnel counts — no raw cross-tenant rows leak. The % math below
    # stays in Python.
    result = await db.execute(
        text("SELECT * FROM public.activation_funnel(:days)"),
        {"days": days},
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
    request: Request,
    current_user: CurrentUser,
    # get_rls_db (not get_db): paid_conversions reads the tenant-scoped
    # referral_events table (policy: referrer_id OR referee_id = GUC). Without
    # the RLS context, under the cutover role that count returns 0 and the
    # referral dashboard underreports. The cross-user `users` uniqueness/count
    # reads here rely on the broad app policy on users, unaffected by the GUC.
    db: AsyncSession = Depends(get_rls_db),
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
    await rate_limit(request, zone="general", identifier=current_user.id)
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
    request: Request,
    current_user: CurrentUser,
) -> dict:
    """Return the user's skip-trace lookup usage + bundled quota.

    Used by the frontend billing page to render a progress bar and
    overage estimate. Values are read from the cached counter on the
    User row — no external calls.
    """
    await rate_limit(request, zone="general", identifier=current_user.id)
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

# price_id → (plan_name, records_limit). Includes BOTH the monthly and annual
# Stripe Price IDs so the webhook maps an annual subscription to the right plan,
# not just the monthly one.
_PRICE_TO_PLAN: dict[str, tuple[str, int]] = {
    pid: (p["id"], p["records_limit"])
    for p in _PLANS
    for pid in (p.get("stripe_price_id"), p.get("stripe_price_id_annual"))
    if pid
}

# Config sanity (log-only — NEVER raise here: a hard failure at import would
# crash-loop the Railway api on boot, per the boot-migration landmine). Warn
# loudly if a configured plan price id is not a Stripe Price ("price_…"); that
# is how a Product id ("prod_…") ended up in a STRIPE_PRICE_* slot before.
for _p in _PLANS:
    for _slot in ("stripe_price_id", "stripe_price_id_annual"):
        _pid = _p.get(_slot)
        if _pid and not _pid.startswith("price_"):
            _logger.warning(
                "billing config: %s for plan '%s' is %r — expected a 'price_' "
                "id; checkout for this plan will fail until Railway env (api AND "
                "worker) is corrected.",
                _slot, _p["id"], _pid,
            )


# ─── Plans catalog ────────────────────────────────────────────────────────────

# 2026-06 pricing migration: founding discount reduced 40% -> 25% so founding
# prices stay above the $99 credibility floor (Pro ~$149.25). New Stripe coupon
# id == "FOUNDING25"; the old 40% coupon "8mX1xa35" was retired in Stripe.
_FOUNDING_COUPON_ID = "FOUNDING25"
_FOUNDING_CACHE_KEY = "founding_offer:FOUNDING25"
_FOUNDING_CACHE_TTL = 60  # seconds


async def _get_founding_offer() -> dict:
    """Return the founding-member offer status (cached).

    REDTEAM B4: the founding coupon was retrieved from Stripe on EVERY hit of
    the PUBLIC, unauthenticated /plans and /pricing endpoints, inside a bare
    `except Exception: pass`. That meant (a) an unauthenticated visitor could
    drive one synchronous Stripe API call per request (latency + a cheap DoS
    amplifier against our Stripe rate limits), and (b) any error — including a
    real Stripe outage — was silently swallowed. This caches the result in
    Redis for ~60s and narrows the except to stripe.error.StripeError, logged
    at warning. On any cache/Stripe failure we fall back to the offer being
    inactive (fail-closed for a promo banner).
    """
    founding = {
        "active": False, "code": "FOUNDING25", "percent_off": 25,
        "spots_total": 25, "spots_remaining": 0,
    }

    import json

    import redis.asyncio as aioredis
    redis = aioredis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
    try:
        cached = await redis.get(_FOUNDING_CACHE_KEY)
        if cached is not None:
            try:
                return json.loads(cached)
            except (ValueError, TypeError):
                pass  # corrupt cache value — recompute below

        try:
            import stripe
            stripe.api_key = settings.STRIPE_SECRET_KEY
            coupon = stripe.Coupon.retrieve(_FOUNDING_COUPON_ID)
            if coupon.valid:
                redeemed = coupon.times_redeemed or 0
                remaining = max(0, (coupon.max_redemptions or 25) - redeemed)
                founding["active"] = remaining > 0
                founding["spots_remaining"] = remaining
        except stripe.error.StripeError as exc:
            # Coupon may not exist, or Stripe is unreachable — offer inactive.
            _logger.warning("founding coupon lookup failed: %s", str(exc)[:200])

        # Cache whatever we computed (active or inactive) to absorb the next
        # ~60s of public traffic without another Stripe round-trip.
        try:
            await redis.set(
                _FOUNDING_CACHE_KEY, json.dumps(founding), ex=_FOUNDING_CACHE_TTL
            )
        except Exception as exc:  # noqa: BLE001 — caching is best-effort
            _logger.warning("founding offer cache write failed: %s", str(exc)[:120])
    except Exception as exc:  # noqa: BLE001 — Redis down must not 500 a public page
        _logger.warning("founding offer cache unavailable: %s", str(exc)[:120])
    finally:
        try:
            await redis.aclose()
        except Exception:  # noqa: BLE001 — best-effort close
            pass

    return founding


@router.get("/plans")
async def list_plans() -> dict:
    """Return the full plan catalog + founding member offer status."""
    founding = await _get_founding_offer()
    return {"plans": _PLANS, "founding_offer": founding}


@router.get("/pricing")
async def pricing_page() -> dict:
    """Return full pricing page data including feature comparison matrix.

    Public endpoint — no auth required. Used by the frontend pricing page.
    """
    founding = await _get_founding_offer()

    return {
        "plans": _PLANS,
        "founding_offer": founding,
        "comparison": {
            "Records per month": {"starter": "50", "pro": "1,000", "business": "5,000", "agency": "Unlimited"},
            "Counties": {"starter": "1", "pro": "5", "business": "Unlimited", "agency": "Unlimited"},
            "Record types": {"starter": "Probate", "pro": "All", "business": "All", "agency": "All"},
            "Data freshness": {"starter": "7-day delay", "pro": "Daily", "business": "Daily", "agency": "Daily"},
            "Export formats": {"starter": "CSV", "pro": "CSV, Excel", "business": "CSV, Excel, JSON, API", "agency": "CSV, Excel, JSON, API"},
            "Scheduling": {"starter": "Manual only", "pro": "Daily, Weekly", "business": "All frequencies", "agency": "All frequencies"},
            "Email delivery": {"starter": False, "pro": True, "business": True, "agency": True},
            "Webhook delivery": {"starter": False, "pro": False, "business": True, "agency": True},
            "Skip tracing": {"starter": False, "pro": "Per-lookup", "business": "1,000 included", "agency": "2,000 included"},
            "API access": {"starter": False, "pro": False, "business": True, "agency": True},
            "Team members": {"starter": "1", "pro": "1", "business": "5", "agency": "Unlimited"},
            "White-label": {"starter": False, "pro": False, "business": False, "agency": "Coming soon"},
            "Support": {"starter": "Community", "pro": "Email", "business": "Priority email", "agency": "Dedicated manager"},
        },
        "trial": {
            "days": 7,
            "plan": "pro",
            "description": "7-day free Pro trial. No credit card required. 1,000 records/month.",
        },
        "faq": [
            {"q": "What are motivated seller leads?", "a": "Public records (probate, foreclosure, tax delinquent, etc.) that indicate a property owner may be willing to sell below market value."},
            {"q": "How fresh is the data?", "a": "We scrape county portals daily. Paid plans get same-day data. Free tier has a 7-day delay."},
            {"q": "What counties do you cover?", "a": "22 Washington State counties are live and scraped daily. We can add any US county in 30 seconds — request yours after signing up."},
            {"q": "Does it include phone and email?", "a": "Yes. Skip tracing is built in — every lead gets phone number, phone type, and email via Tracerfy within 10-15 minutes."},
            {"q": "Can I cancel anytime?", "a": "Yes. No contracts, no cancellation fees. Your data exports remain available for 30 days after cancellation."},
            {"q": "What export formats do you support?", "a": "CSV, Excel, and JSON. Business and Agency plans also get API access for direct integration."},
        ],
    }


# ─── Usage ────────────────────────────────────────────────────────────────────

@router.get("/usage")
async def get_usage(request: Request, current_user: CurrentUser) -> dict:
    """Return current plan, record usage, and limit for the settings page."""
    await rate_limit(request, zone="general", identifier=current_user.id)
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
async def get_subscription(request: Request, current_user: CurrentUser) -> dict:
    """Return the user's active Stripe subscription details, if any."""
    await rate_limit(request, zone="stripe", identifier=current_user.id)
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
    except stripe.error.StripeError:
        # Never surface Stripe's user_message to the client — it can disclose
        # Stripe-side state/config. Log server-side, return a generic message.
        _logger.exception("subscription lookup failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not retrieve subscription. Please try again.",
        )


# ─── Checkout ─────────────────────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    price_id: str


@router.post("/checkout")
async def create_checkout(
    request: Request,
    body: CheckoutRequest,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> dict:
    """Create a Stripe Checkout session to upgrade the user's plan."""
    # Tighter cap than a plain read: each call hits Stripe (Customer + Checkout
    # Session creation), so loop-abuse spams Stripe + the operator's quota.
    await rate_limit(request, zone="stripe", identifier=current_user.id)
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

    # Defensive: the resolved id MUST be a Stripe Price ("price_…"), never a
    # Product ("prod_…"). A misconfigured STRIPE_PRICE_* env (a product id in a
    # price slot) would otherwise reach Stripe and surface as a generic 502;
    # fail fast with a logged config error and a clean message instead.
    if not stripe_price_id.startswith("price_"):
        _logger.error(
            "checkout: resolved id %r is not a 'price_' id — STRIPE_PRICE_* is "
            "misconfigured (check Railway env on api AND worker).",
            stripe_price_id,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing is temporarily unavailable. Please try again later.",
        )

    try:
        customer_id = current_user.stripe_customer_id
        if not customer_id:
            # C3 (full-SaaS review): check Stripe for an existing
            # customer with this email before creating a new one.
            # If one exists AND its metadata matches this user_id,
            # adopt it — prevents orphaned Stripe customers when a
            # user completes checkout in multiple browser tabs or
            # when a prior checkout was abandoned after customer
            # creation but before success. If an existing customer
            # has mismatched metadata, we refuse to adopt and
            # create a fresh one so we never reuse another
            # account's Stripe customer.
            existing = stripe.Customer.list(email=current_user.email, limit=5)
            reusable = None
            for c in (existing.get("data") or []):
                md_user_id = (c.get("metadata") or {}).get("user_id")
                if md_user_id == current_user.id:
                    reusable = c
                    break
            if reusable is not None:
                customer_id = reusable["id"]
            else:
                customer = stripe.Customer.create(
                    email=current_user.email,
                    metadata={"user_id": current_user.id},
                )
                customer_id = customer["id"]
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
    except Exception:
        _logger.exception("Checkout failed for user %s", current_user.id)
        raise HTTPException(status_code=502, detail="Checkout temporarily unavailable")


# ─── Customer portal ──────────────────────────────────────────────────────────

@router.post("/portal")
async def customer_portal(request: Request, current_user: CurrentUser) -> dict:
    """Return a Stripe Customer Portal URL for managing subscriptions."""
    await rate_limit(request, zone="stripe", identifier=current_user.id)
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
    redis = aioredis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
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

def _alert_billing_gap(reason: str, dedup_key: str, **ctx: object) -> None:
    """Loudly surface a webhook event that silently dropped payment/entitlement state.

    The handlers below used to ``return`` silently when a Stripe price wasn't in
    _PRICE_TO_PLAN — so a PAID subscription whose price drifted out of the
    STRIPE_PRICE_* env (new/changed/legacy price) would never activate the user's
    plan, with no log or alert. This logs at ERROR with full recovery identifiers
    (Stripe price/customer/session/subscription ids + our user_id — all non-PII,
    never email) and fires a deduped ops alert. Defensive: an alerting failure must
    NEVER propagate — the webhook must still return 200, else Stripe retries an
    event we already processed. The ops dedup key is per price so one bad price
    can't spam ops, while the per-occurrence ERROR log keeps every affected user
    visible. NEVER grants a fallback plan — wrong entitlement is worse than missing.
    """
    ctx_str = " ".join(f"{k}={v}" for k, v in ctx.items() if v)
    _logger.error("billing webhook gap: %s — %s", reason, ctx_str)
    try:
        from src.workers.ops_alerts import send_ops_alert

        send_ops_alert(
            "billing",
            dedup_key,
            f"Billing webhook gap: {reason}",
            f"{reason}\n{ctx_str}\n\nManual recovery may be needed.",
        )
    except Exception:  # alerting must never fail the webhook (Stripe would retry)
        _logger.exception("failed to send billing-gap ops alert (%s)", reason)


async def _handle_checkout_completed(data: dict, db: AsyncSession) -> None:
    """Activate new plan after a successful checkout session."""
    user_id = (data.get("metadata") or {}).get("user_id")
    subscription_id = data.get("subscription")
    session_customer_id = data.get("customer")

    if not user_id or not subscription_id:
        return

    subscription = stripe.Subscription.retrieve(
        subscription_id,
        expand=["items.data.price"],
    )
    price_id = subscription["items"]["data"][0]["price"]["id"]
    plan_info = _PRICE_TO_PLAN.get(price_id)

    if not plan_info:
        # Paid checkout but the price isn't in our plan map — entitlement would be
        # silently lost. Alert with recovery ids; do NOT grant a fallback plan.
        _alert_billing_gap(
            "checkout.session.completed price not in plan map — user PAID but "
            "plan NOT activated",
            f"unmapped-price:{price_id}",
            event=data.get("id"),
            price_id=price_id,
            customer=session_customer_id,
            subscription=subscription_id,
            user_id=user_id,
        )
        return

    plan_name, records_limit = plan_info
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        _logger.warning(
            "checkout.session.completed: user %s not found — session=%s",
            user_id, data.get("id"),
        )
        return

    # C3 (full-SaaS review): verify that the Stripe customer on
    # this session matches (or can be bound to) this BridgeLeads
    # user. Without this check, a session whose metadata.user_id
    # was tampered with — or a webhook replayed after the caller
    # changed the user's stripe_customer_id via a second checkout
    # flow — could grant a plan to the wrong account. If the user
    # already has a stripe_customer_id set, it must match the
    # session customer. If not set, we bind it now.
    if session_customer_id:
        if user.stripe_customer_id and user.stripe_customer_id != session_customer_id:
            _logger.error(
                "checkout.session.completed: customer_id mismatch for user "
                "%s — session customer=%s, stored customer=%s. Refusing to "
                "apply plan change.",
                user_id, session_customer_id, user.stripe_customer_id,
            )
            return
        if not user.stripe_customer_id:
            user.stripe_customer_id = session_customer_id

    user.plan = plan_name
    user.records_limit = records_limit
    # Durable entitlement (migration 077) + end the app-side 7-day trial: a
    # converted user is no longer "on trial", so expire_trials must not consider
    # them. subscription["status"] is authoritative (retrieved above).
    user.stripe_subscription_id = subscription_id
    user.subscription_status = subscription.get("status")
    user.trial_ends_at = None
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

    Delegates to the SECURITY DEFINER public.grant_referral_credit() (migration
    029). The Stripe webhook runs with NO per-user RLS context and the referral
    row spans TWO users (referrer + referee), so under the NOBYPASSRLS
    bridgeleads_app role the app role cannot write referral_events directly.
    The function — owned by a privileged role, EXECUTE granted only to
    bridgeleads_app — resolves the referrer from users.referred_by_user_id,
    inserts the audit row idempotently (unique(referee_id) → a Stripe replay is
    a no-op) and increments the referrer's balance atomically. A no-op when the
    referee has no referrer or the referrer was deleted.

    The prior savepoint/IntegrityError dance is now handled inside the function
    by ON CONFLICT (referee_id) DO NOTHING, so the enclosing webhook transaction
    (the plan upgrade flushed by _handle_checkout_completed) is never disturbed.
    """
    await db.execute(
        text("SELECT public.grant_referral_credit(:referee_id)"),
        {"referee_id": str(referee.id)},
    )
    _logger.info("referral: grant_referral_credit processed referee=%s", referee.id)


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
        # Subscription changed to a price we don't map — the plan change would be
        # silently lost. Alert with recovery ids; do NOT guess a plan.
        _alert_billing_gap(
            "customer.subscription.updated price not in plan map — plan change "
            "NOT applied",
            f"unmapped-price:{price_id}",
            price_id=price_id,
            customer=customer_id,
        )
        return

    plan_name, records_limit = plan_info
    result = await db.execute(select(User).where(User.stripe_customer_id == customer_id))
    user = result.scalar_one_or_none()
    if user is None:
        # A real plan change for a customer we can't resolve to a user — lost
        # silently before. Loud warning (no ops page: often a benign unknown
        # customer, lower severity than a paid-but-unmapped price).
        _logger.warning(
            "customer.subscription.updated: no user for stripe_customer_id=%s — "
            "plan change to %s (limit %s) NOT applied",
            customer_id, plan_name, records_limit,
        )
        return
    user.plan = plan_name
    user.records_limit = records_limit
    # Keep the durable entitlement state in sync (migration 077). An entitled
    # status ends the app-side trial so expire_trials won't downgrade a payer.
    user.stripe_subscription_id = data.get("id")
    user.subscription_status = data.get("status")
    if data.get("status") in ("active", "trialing"):
        user.trial_ends_at = None
    await db.flush()
    from src.api.entitlements import apply_reconciliation_async
    await apply_reconciliation_async(db, str(user.id), user.plan)


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
        # Clear the entitlement so any future trial logic treats them as unpaid.
        user.stripe_subscription_id = None
        user.subscription_status = "canceled"
        await db.flush()
        from src.api.entitlements import apply_reconciliation_async
        await apply_reconciliation_async(db, str(user.id), user.plan)


async def _handle_payment_failed(data: dict, db: AsyncSession) -> None:
    """Send a payment failure notification email via Resend."""
    customer_id = data.get("customer")

    # REDTEAM B3: clamp the webhook-supplied attempt_count before it flows
    # into the email body / logs. Stripe normally sends a small integer, but
    # the value is attacker-influenceable on a forged-but-replayed payload and
    # was previously passed through unbounded. Coerce to int and bound to
    # [1, 20]; anything non-numeric falls back to 1.
    try:
        attempt_count = max(1, min(int(data.get("attempt_count", 1)), 20))
    except (TypeError, ValueError):
        attempt_count = 1

    if not customer_id:
        return

    result = await db.execute(select(User).where(User.stripe_customer_id == customer_id))
    user = result.scalar_one_or_none()
    if not user:
        return

    # Send notification — imported here to avoid circular at startup
    from src.workers.delivery import _send_payment_failed_email
    _send_payment_failed_email(user.email, attempt_count)

    # Phase 2b: best-effort in-app notification via the worker/system path
    # (the webhook session has no user RLS GUC — never write notifications here).
    try:
        from src.workers.tasks import emit_payment_notification
        emit_payment_notification.delay(str(user.id), attempt_count)
    except Exception as exc:  # enqueue failure must not fail the webhook
        _logger.warning("payment notification enqueue failed (non-fatal): %s", exc)
