"""Stripe billing routes: checkout, portal, webhooks, plans, usage."""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial

import stripe
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import CurrentUser, require_admin
from src.api.billing_entitlement import (
    _rank,
    activate_paid_plan,
    apply_plan_change,
    end_subscription,
    mark_payment_failed,
)
from src.api.deps import get_rls_db
from src.api.middleware import client_ip, rate_limit
from src.api.schemas import RunEligibilityResponse, UsageResponse
from src.config import frontend_routes, settings
from src.config.constants import (
    ALL_RECORD_TYPES,
    ALL_SCHEDULE_FREQUENCIES,
    BATCH_PLANS,
    BUSINESS_FEATURES_PLANS,
    COUNTY_LIMIT_BY_PLAN,
    OVERLAP_PLANS,
    PRIORITY_QUEUE_PLANS,
    RECORD_TYPES_BY_PLAN,
    SUPPORTED_EXPORT_FORMATS,
    TRIAL_PERIOD_DAYS,
    allowed_export_formats,
    allowed_schedule_frequencies,
    export_format_label,
    normalize_plan,
    record_type_label,
)
from src.config.plans import PLAN_CATALOG
from src.config.stripe_client import configure_stripe
from src.db import User, get_db
from src.utils.logger import setup_logger

_logger = setup_logger("billing")

configure_stripe()

router = APIRouter(prefix="/billing", tags=["billing"])


def step_conversions(
    *,
    signups: int,
    first_scraper: int,
    first_job: int,
    first_download: int,
    scraper_and_job: int,
    job_and_download: int,
    downloaded_and_paid: int,
) -> dict[str, float]:
    """Participation overlap between funnel stages.

    PRECONDITION: the intersections come from activation_funnel_v2, where each is
    a COUNT(*) FILTER over the same one-row-per-user CTE and so is bounded by both
    of its populations by construction. Given that, every value is 0-100. This
    does NOT clamp: handed an intersection larger than its population it will
    return more than 100, on purpose, because that would mean the SQL had broken
    and hiding it behind a min() is how a wrong funnel goes unnoticed.

    Each rate is "of the users at stage A, the share who are ALSO at stage B",
    built from a real intersection (migration 091). It is NOT an ordered
    conversion: this data cannot show that somebody paid AFTER downloading,
    because plan and stripe_customer_id are current values and migration 088
    backfilled first_paid_at from created_at.

    Dividing one stage COUNT by another is what this replaces. Those counts are
    independent marginals over populations that are not nested: the download
    stamp lives on `users` and outlives that user's job rows, and being on a paid
    plan has nothing to do with downloading. Two paid users and one downloader
    reported 200%.

    signup_to_scraper stays a plain share of signups, because every stage here is
    drawn from the signup cohort by construction, so that one IS nested.

    Lives out here, not inside the handler, so it can be tested without an
    admin+MFA HTTP round trip. Inline, reverting it to the marginal division left
    the whole suite green.
    """
    def _share(part: int, whole: int) -> float:
        return round(100 * part / whole, 1) if whole else 0.0

    return {
        "signup_to_scraper": _share(first_scraper, signups),
        "scraper_to_job": _share(scraper_and_job, first_scraper),
        "job_to_download": _share(job_and_download, first_job),
        "download_to_paid": _share(downloaded_and_paid, first_download),
    }


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

    Returns the activation funnel across the last `days` days:
      signup -> first scraper -> first job -> first download -> paid upgrade

    All derived from existing tables. Each step shows an absolute count and its
    share of signups.

    What the window actually means, because it is easy to read too much into it:
    this is the CURRENT state of currently-active users who SIGNED UP in the last
    `days` days. It is not "events in the last N days", and not "converted within
    N days of signing up". Plan, stripe_customer_id and first_leads_downloaded_at
    are current values, so a user who upgrades today moves the bar for the window
    they signed up in. Recent signups have had less time to progress, and download
    observation only begins at migration 090, so cohorts older than that read as
    not-downloaded until they age out.

    step_conversions are participation OVERLAP, not ordered conversion: "of the
    users at stage A, the share who are also at stage B". They come from real
    intersections (migration 091) rather than one stage count divided by another,
    which is what let download_to_paid report 200%.

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
        text("SELECT * FROM public.activation_funnel_v2(:days)"),
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
    # Intersections (migration 091). A stage count on its own is a marginal, and
    # dividing one marginal by another only reads as a rate when one population
    # is provably inside the other. These are not: the download stamp lives on
    # `users` and survives its jobs, and paid is a CURRENT plan check that has
    # nothing to do with downloading. Dividing all paid users by downloaders is
    # what produced 200%.
    scraper_and_job = row.scraper_and_job or 0
    job_and_download = row.job_and_download or 0
    downloaded_and_paid = row.downloaded_and_paid or 0

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
        "step_conversions": step_conversions(
            signups=signups,
            first_scraper=first_scraper,
            first_job=first_job,
            first_download=first_download,
            scraper_and_job=scraper_and_job,
            job_and_download=job_and_download,
            downloaded_and_paid=downloaded_and_paid,
        ),
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
    # /signup is not a page and is not public: a prospect following the shared
    # link was bounced to /login and the ref code was dropped. /register is both,
    # and it reads ?ref= at mount.
    share_url = f"{base}{frontend_routes.referral_signup(user.referral_code)}"

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

    The period reported is the user's ENTITLEMENT WINDOW, the same one records
    are metered over and the same one the counter now rolls on. It used to
    report the raw `skip_trace_period_start`, which was a calendar month, so the
    page told a subscriber anchored on the 20th that their lookups reset on the
    1st while their records reset on the 20th and Stripe invoiced on the 20th.
    `effective_window` is used rather than the stored pair for the same reason
    /billing/usage uses it: a window that has ended but not yet rolled would
    otherwise be reported as the current one.
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

    from src.api.quota import effective_window

    _window_start, _window_end = effective_window(current_user)

    return {
        "plan": plan,
        "quota": quota,
        "used": used,
        "remaining": max(0, quota - used) if quota > 0 else None,
        "overage_units": overage_units,
        "overage_rate_usd": overage_rate_usd,
        "estimated_charges_usd": estimated_charges_usd,
        "period_start": _window_start.isoformat(),
        "period_end": _window_end.isoformat(),
    }

# ─── Plan catalog ─────────────────────────────────────────────────────────────

# The catalog itself now lives in src/config/plans.py so the Celery workers
# that quote a price or a record allowance in a transactional email read the
# SAME numbers these endpoints serve. (The trial-expiry email had drifted to a
# hardcoded "$79/mo" while Pro was $199.) This alias keeps the module-local
# name, and plans.py holds this catalog verbatim.
_PLANS = PLAN_CATALOG

# price_id → (plan_name, records_limit, interval). Includes BOTH the monthly and
# annual Stripe Price IDs so the webhook maps an annual subscription to the right
# plan, not just the monthly one.
#
# The INTERVAL is now carried because the map used to collapse monthly and annual
# onto the same tuple, leaving the code unable to tell them apart. It is NOT used
# to size the entitlement window — that is always one month, so an annual Pro
# subscriber gets twelve 1,000-record windows rather than 1,000 records for a
# year — but it is needed to report the subscription honestly and to recognise a
# monthly<->annual switch as a no-op for quota rather than as a plan change.
_PRICE_TO_PLAN: dict[str, tuple[str, int, str]] = {
    pid: (p["id"], p["records_limit"], interval)
    for p in _PLANS
    for pid, interval in (
        (p.get("stripe_price_id"), "month"),
        (p.get("stripe_price_id_annual"), "year"),
    )
    if pid
}


def _legacy_plan_prices(raw: str, sold: dict) -> dict[str, tuple[str, int, str]]:
    """Retired plan prices from STRIPE_LEGACY_PLAN_PRICES, for existing subscriptions.

    A Stripe price cannot move between Products, so re-issuing a plan price on
    a new Product (which the single-customer promotion needs) leaves current
    subscribers on the old id. Without this map the webhooks would stop
    recognising their plan. Malformed entries are logged and skipped, never
    raised: a failure at import would crash-loop the api on boot.
    """
    # Paid plans only: the free tier has no Stripe price to be legacy of.
    by_plan = {p["id"]: p["records_limit"] for p in _PLANS if p.get("price_monthly")}
    out: dict[str, tuple[str, int, str]] = {}
    ids = [e.split(":")[0].strip() for e in (raw or "").split(",") if e.strip()]
    # The same price listed twice with different plans would make entitlement
    # depend on entry order; neither entry is trusted.
    conflicted = {pid for pid in ids if ids.count(pid) > 1}
    for entry in (e.strip() for e in (raw or "").split(",") if e.strip()):
        if entry.split(":")[0].strip() in conflicted:
            _logger.warning(
                "billing config: STRIPE_LEGACY_PLAN_PRICES lists %r more than once; "
                "ignored", entry.split(":")[0].strip(),
            )
            continue
        parts = [s.strip() for s in entry.split(":")]
        if (
            len(parts) != 3
            or not parts[0].startswith("price_")
            or parts[1] not in by_plan
            or parts[2] not in ("month", "year")
            or parts[0] in sold
        ):
            _logger.warning(
                "billing config: STRIPE_LEGACY_PLAN_PRICES entry %r ignored (expected "
                "price_id:plan:interval for a paid plan, not a currently sold price)",
                entry,
            )
            continue
        out[parts[0]] = (parts[1], by_plan[parts[1]], parts[2])
    return out


# Prices recognised on existing subscriptions but never sold again.
_LEGACY_PRICE_TO_PLAN = _legacy_plan_prices(settings.STRIPE_LEGACY_PLAN_PRICES, _PRICE_TO_PLAN)
_PRICE_TO_PLAN.update(_LEGACY_PRICE_TO_PLAN)

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


# ── Comparison-table cells, derived from the enforced matrix ─────────────────
# Every one of these used to be a hand-typed string, and three of them had
# drifted away from the gate they describe. Deriving costs a few lines and
# makes the drift impossible rather than merely unlikely.

def _record_types_cell(plan: str) -> str:
    allowed = RECORD_TYPES_BY_PLAN[plan]
    if allowed == ALL_RECORD_TYPES:
        return "All"
    return ", ".join(sorted(record_type_label(rt) for rt in allowed))


def _export_formats_cell(plan: str) -> str:
    allowed = allowed_export_formats(plan)
    if allowed == SUPPORTED_EXPORT_FORMATS:
        return "All formats"
    # xlsx is the on-disk alias of excel; one format, one label.
    return ", ".join(sorted({export_format_label(f) for f in allowed}))


def _scheduling_cell(plan: str) -> str:
    allowed = allowed_schedule_frequencies(plan)
    recurring = sorted(allowed - {"manual"})
    if not recurring:
        return "Manual only"
    if allowed == ALL_SCHEDULE_FREQUENCIES:
        return "All frequencies"
    return ", ".join(f.title() for f in recurring)


def _skip_trace_cell(plan: str) -> str | bool:
    quota = settings.SKIP_TRACE_BUNDLED_QUOTAS.get(plan, 0)
    return f"{quota:,} included" if quota else False


@router.get("/pricing")
async def pricing_page() -> dict:
    """Return full pricing page data including feature comparison matrix.

    Public endpoint — no auth required. Used by the frontend pricing page.
    """
    return {
        "plans": _PLANS,
        "founding_offer": await _get_founding_offer(),
        **_pricing_tables(),
    }


def _pricing_tables() -> dict:
    """The comparison matrix, trial and FAQ that /billing/pricing serves.

    Pure (no Stripe, no Redis), so the tests that hold these cells to the
    gates do not depend on the founding-offer lookup in pricing_page.
    """
    return {
        "comparison": {
            # Derived from the quota the gate enforces, like the rows below.
            "Records per month": {
                plan: ("Unlimited" if settings.PLAN_LIMITS[plan] < 0
                       else f"{settings.PLAN_LIMITS[plan]:,}")
                for plan in ("starter", "pro", "business", "agency")
            },
            # Derived from the ENFORCED cap, never re-typed. These cells had
            # drifted to the pre-2026-06 pricing (pro "5", business
            # "Unlimited") while COUNTY_LIMIT_BY_PLAN caps them at 3 and 10.
            # With ENTITLEMENT_ENFORCEMENT on in production that is not a
            # cosmetic typo: the page promised counties the API answers 402 for.
            "Counties": {
                plan: ("Unlimited" if COUNTY_LIMIT_BY_PLAN[plan] < 0
                       else f"{COUNTY_LIMIT_BY_PLAN[plan]:,}")
                for plan in ("starter", "pro", "business", "agency")
            },
            # Derived, never re-typed. This row said Pro got "All" record types
            # while RECORD_TYPES_BY_PLAN gives it four of seven and the API
            # answers 402 for the rest, in the SAME response whose plan bullets
            # named the correct four. The Counties row above had drifted the same
            # way and was fixed the same way in #235.
            "Record types": {
                plan: _record_types_cell(plan)
                for plan in ("starter", "pro", "business", "agency")
            },
            "Data freshness": {"starter": "7-day delay", "pro": "Daily", "business": "Daily", "agency": "Daily"},
            "Export formats": {
                plan: _export_formats_cell(plan)
                for plan in ("starter", "pro", "business", "agency")
            },
            "Scheduling": {
                plan: _scheduling_cell(plan)
                for plan in ("starter", "pro", "business", "agency")
            },
            # Starter read False here and nothing enforced it: `deliver.emails` is
            # accepted on every plan, and the Starter card never claimed otherwise.
            # A row nothing implements is a promise in the wrong direction.
            "Email delivery": {"starter": True, "pro": True, "business": True, "agency": True},
            # Webhook, dialer and API access share one gate (scrapers.py refuses
            # webhook/dialer delivery, auth.py refuses API keys, below Business).
            "Webhook delivery": {
                plan: plan in BUSINESS_FEATURES_PLANS
                for plan in ("starter", "pro", "business", "agency")
            },
            "Dialer delivery": {
                plan: plan in BUSINESS_FEATURES_PLANS
                for plan in ("starter", "pro", "business", "agency")
            },
            # Pro read "Per-lookup", which dropped the 250 lookups its own card
            # bullet includes. Derived from the same quotas the meter bills on.
            "Skip tracing": {
                plan: _skip_trace_cell(plan)
                for plan in ("starter", "pro", "business", "agency")
            },
            "Overlap and intersection lists": {
                plan: plan in OVERLAP_PLANS
                for plan in ("starter", "pro", "business", "agency")
            },
            "Batch scraping": {
                plan: plan in BATCH_PLANS
                for plan in ("starter", "pro", "business", "agency")
            },
            "API access": {
                plan: plan in BUSINESS_FEATURES_PLANS
                for plan in ("starter", "pro", "business", "agency")
            },
            "Priority queue": {
                plan: plan in PRIORITY_QUEUE_PLANS
                for plan in ("starter", "pro", "business", "agency")
            },
            # "Team members" used to sit here as 1 / 1 / 5 / Unlimited. There is no
            # seat model anywhere in this application: no invite flow, no member
            # table, no route. The row was a promise with nothing behind it, so it
            # is gone rather than restated. Put it back when seats exist.
            "White-label": {"starter": False, "pro": False, "business": False, "agency": "Coming soon"},
            "Support": {"starter": "Community", "pro": "Email", "business": "Priority email", "agency": "Dedicated manager"},
        },
        # Read from the same constants registration stamps and the welcome email
        # quotes, so the three cannot drift apart the way the trial email's
        # "$79/mo" drifted from the catalog's $199. The served values are
        # unchanged (TRIAL_PERIOD_DAYS is 7, Pro is 1,000 records).
        "trial": {
            "days": TRIAL_PERIOD_DAYS,
            "plan": "pro",
            "description": (
                f"{TRIAL_PERIOD_DAYS}-day free Pro trial. No credit card "
                f"required. {settings.PLAN_LIMITS['pro']:,} records/month."
            ),
        },
        "faq": [
            {"q": "What are motivated seller leads?", "a": "Public records (probate, foreclosure, tax delinquent, etc.) that indicate a property owner may be willing to sell below market value."},
            # The 7-day Starter delay is real: tasks_helpers/dates.py ends a
            # Starter run's window seven days back (test_plan_entitlement_audit).
            {"q": "How fresh is the data?", "a": "We read county portals on the schedule you set. Paid plans get records up to the current day; Starter's data is delayed 7 days."},
            # No count here: coverage changes as connectors go live or degrade,
            # and "22 counties" / "any US county in 30 seconds" had drifted from
            # it. /scrapers/connectors (the coverage page) is the live answer.
            {"q": "What counties do you cover?", "a": "Washington State counties, with more added over time. The live list, with the record types each county covers, is on the coverage page."},
            # Never imply every lookup finds contacts: a trace can come back with
            # fewer or none. Starter has no lookup allowance (Skip tracing row).
            {"q": "Does it include phone and email?", "a": "On Pro, Business and Agency, BridgeLeads looks up the owner's phone and email. Each plan includes a monthly allowance, then lookups are billed per use. A lookup can come back with fewer contacts or none. Starter does not include lookups."},
            # "Exports remain available for 30 days after cancellation" was not
            # implemented anywhere (no cancellation expiry; retention is separate).
            {"q": "Can I cancel anytime?", "a": "Yes. No contracts, no cancellation fees."},
            {"q": "What export formats do you support?", "a": "CSV, Excel, and JSON. Each run delivers one file in the format you pick. Starter is CSV, Pro adds Excel, and Business and Agency get every format plus API access for direct integration."},
        ],
    }


# ─── Usage ────────────────────────────────────────────────────────────────────

def usage_view(user: User, now: datetime) -> dict:
    """What ``/billing/usage`` reports for ``user`` at ``now``.

    ONE clock: every helper is handed the same ``now``, so a request that
    straddles a window boundary cannot report the old window's reset date next
    to the new window's usage.

    The plan and limit are the ones the gate and the next charge act on. When
    the window has ended and the lazy rollover has not run yet, that rollover
    will apply a pending downgrade — so this mirrors the rollover statement
    (``quota_window.py``, the ``plan = CASE WHEN rolling AND pending_plan IS
    NOT NULL`` assignment) exactly: the pending plan becomes the plan, and the
    pending pair is cleared.
    """
    from src.api.quota import (
        effective_records_limit,
        effective_records_used,
        effective_window,
        is_frozen,
        next_quota_reset,
        run_eligibility,
    )
    from src.api.quota_window import as_utc, should_roll

    rolling = should_roll(user, now)
    limit = effective_records_limit(user, now)
    used = effective_records_used(user, now)
    period_start, _period_end = effective_window(user, now)
    ends_at = user.entitlement_ends_at
    return {
        "plan": (
            user.pending_plan
            if rolling and user.pending_plan is not None
            else user.plan
        ),
        "records_used": used,
        "records_limit": limit,
        "records_remaining": max(0, limit - used) if limit != -1 else None,
        "percent_used": round((used / limit) * 100, 1) if limit and limit != -1 else 0,
        "period_start": as_utc(period_start),
        # The window END is the reset instant: the boundary belongs to the NEW
        # window. None when paid access stops at or before it (cancel at period
        # end): that boundary ends the subscription, it does not reset it.
        "next_reset_at": next_quota_reset(user, now),
        "period_basis": "entitlement_month_utc",
        # A pending downgrade is visible but NOT yet applied — the customer keeps
        # the cap they paid for until the boundary above. Once that boundary has
        # passed it is no longer pending: see above.
        "pending_plan": None if rolling else user.pending_plan,
        "pending_records_limit": None if rolling else user.pending_records_limit,
        "payment_state": "frozen" if is_frozen(user, now) else "ok",
        "entitlement_ends_at": as_utc(ends_at) if ends_at else None,
        "run_eligibility": RunEligibilityResponse.model_validate(
            run_eligibility(user, now)
        ),
    }


@router.get("/usage", response_model=UsageResponse)
async def get_usage(request: Request, current_user: CurrentUser) -> dict:
    """Return current plan, record usage, limit, and the ENTITLEMENT WINDOW.

    Quota no longer resets on the 1st. It resets on the user's own entitlement
    anniversary — the monthly grid anchored at ``users.quota_anchor_at`` — so a
    subscriber who starts on the 20th is metered from the 20th and an annual
    subscriber still gets a fresh month every month.

    The window reported here is the EFFECTIVE one, not the stored pair. Rollover
    is lazy: it happens inside the statement that next charges the user, with an
    hourly reconciliation for people who never transact. Between a boundary and
    whichever of those comes first, the stored window has legitimately expired,
    and echoing it would tell a user their quota resets on a date that has
    already passed. ``records_used`` is window-aware for the same reason the
    enforcement gates are — reporting the raw column would show usage they no
    longer owe.

    ``payment_state`` is reported separately from usage on purpose: a customer
    frozen for a failed payment is not "over their limit", and sending them to
    the upgrade page would not fix anything.

    ``run_eligibility`` is the same answer every enqueue gate gives
    (``src.api.quota.run_eligibility``), so the page never has to re-derive it
    from the fields above.
    """
    await rate_limit(request, zone="general", identifier=current_user.id)
    return usage_view(current_user, datetime.now(UTC))


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
        # The LICENSED plan item, not items[0]: the metered skip-trace item
        # sits on the same subscription and has unit_amount 8 (cents per
        # lookup), which as "amount_monthly" would read to the customer as a
        # $0 plan.
        _items = sub["items"]["data"]
        _plan_price_id = _plan_item_price_id(_items)
        price = next(
            (i["price"] for i in _items if i["price"]["id"] == _plan_price_id),
            _items[0]["price"],
        )
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

# plan id -> the metered skip-trace Price for each billing interval.
_SKIP_TRACE_METERED_PRICE: dict[str, dict[str, str]] = {
    "pro": {
        "month": settings.STRIPE_PRICE_SKIP_TRACE_PRO,
        "year": settings.STRIPE_PRICE_SKIP_TRACE_PRO_ANNUAL,
    },
    "business": {
        "month": settings.STRIPE_PRICE_SKIP_TRACE_BUSINESS_OVERAGE,
        "year": settings.STRIPE_PRICE_SKIP_TRACE_BUSINESS_ANNUAL,
    },
    "agency": {
        "month": settings.STRIPE_PRICE_SKIP_TRACE_AGENCY_OVERAGE,
        "year": settings.STRIPE_PRICE_SKIP_TRACE_AGENCY_ANNUAL,
    },
}


def _metered_skip_trace_price(plan: str, interval: str) -> str | None:
    """The metered skip-trace Price to put on this subscription, or None.

    None is a deliberate, survivable outcome, not an error:

      * Starter has no skip-trace allowance and no metered price;
      * an interval with no provisioned price (today: every annual one)
        would otherwise fail the whole checkout, because Stripe requires
        every item in a subscription to share one recurring interval and
        the monthly price cannot ride on a yearly subscription.

    Selling a plan is more important than metering its overage, so an
    unprovisioned interval logs loudly and checkout proceeds unmetered.
    """
    price = (_SKIP_TRACE_METERED_PRICE.get(plan) or {}).get(interval, "")
    if not price:
        if plan in _SKIP_TRACE_METERED_PRICE:
            _logger.warning(
                "checkout: no metered skip-trace price configured for plan %s on "
                "a %sly subscription. Over-quota lookups will be recorded and "
                "NOT billed. Provision one and set the matching STRIPE_PRICE_"
                "SKIP_TRACE_* env on api AND worker.",
                plan, interval,
            )
        return None
    if not price.startswith("price_"):
        _logger.error(
            "checkout: metered skip-trace id %r for plan %s is not a 'price_' "
            "id. Skipping it rather than failing the sale.",
            price, plan,
        )
        return None
    return price


def _plan_item_price_id(items: list) -> str | None:
    """The LICENSED plan price among a subscription's items.

    Every reader here used to take ``items[0]``, which was safe only while a
    subscription had exactly one item. With the metered skip-trace item
    attached, index 0 is whichever Stripe returns first, so a plan lookup on
    it would miss the map, alert "price not in plan map", and refuse to
    activate a plan the customer had just paid for.
    """
    for item in items or []:
        pid = ((item or {}).get("price") or {}).get("id")
        if pid and pid in _PRICE_TO_PLAN:
            return pid
    return None


# ─── Checkout guard: one subscription per customer ────────────────────────────

# A subscription in any of these states is OVER. Everything else is a live
# obligation, including the ones that are easy to read as "not really active":
# `past_due` and `unpaid` still bill, `paused` still exists, `trialing` becomes
# active on its own, and `incomplete` can still be paid within Stripe's ~23h
# initial-payment window. Anything Stripe adds later is unknown to this set and
# therefore blocks — the set names what is SAFE, so a new status fails closed.
_TERMINAL_SUBSCRIPTION_STATUSES = frozenset({"canceled", "incomplete_expired"})


class _StripeStateUnavailableError(Exception):
    """Stripe could not be asked what subscriptions this customer has.

    Raised rather than swallowed: not knowing is not the same as knowing there
    is none, and treating an outage as "no subscription" is exactly how a
    customer ends up paying twice.
    """


def _resolve_existing_customer(email: str, user_id: str) -> str | None:
    """The Stripe customer already belonging to this user, or None.

    Paginated. This used to read `Customer.list(email=..., limit=5)` and take
    the first metadata match in that page, so a user with more than five Stripe
    customers on one address could have their real one sit on page two: we
    would create a SIXTH customer, and the subscription guard below would then
    enumerate the wrong customer's subscriptions and wave through a duplicate
    subscription. The guard is only as good as the customer it is pointed at.

    Metadata must match this user_id exactly. A customer with someone else's
    user_id, or with none, is never adopted — reusing another account's Stripe
    customer would cross-bill two tenants.
    """
    for customer in stripe.Customer.list(email=email, limit=100).auto_paging_iter():
        if ((customer.get("metadata") or {}).get("user_id")) == user_id:
            return customer["id"]
    return None


def _live_subscription(customer_id: str) -> dict | None:
    """The customer's first non-terminal subscription, or None.

    `status="all"` is REQUIRED, not defensive. The Stripe SDK documents the
    default as "all subscriptions that have not been canceled" — which sounds
    like what we want and is not: it omits `canceled` (fine) but the point of
    this call is to see EVERY state, and relying on an unstated default to pick
    the right ones is how the wrong set gets enumerated after an SDK bump.

    Deliberately NOT filtered by price: the question is "does this customer
    already owe Stripe money on a subscription", not "does this customer
    already have the plan they just clicked". Buying annual while holding
    monthly is the exact case that produced two live obligations.
    """
    try:
        for sub in stripe.Subscription.list(
            customer=customer_id, status="all", limit=100,
        ).auto_paging_iter():
            if sub.get("status") not in _TERMINAL_SUBSCRIPTION_STATUSES:
                return sub
    except Exception as exc:  # noqa: BLE001 — any Stripe/network failure is "unknown"
        raise _StripeStateUnavailableError(str(exc)[:200]) from exc
    return None


def _expire_open_checkout_sessions(customer_id: str) -> int:
    """Expire this customer's open subscription-mode Checkout Sessions.

    The advisory lock below serialises two concurrent /checkout CALLS; it does
    nothing about two Sessions that already exist. A session stays purchasable
    until it expires on its own, so a customer with two tabs open can pay for
    both and end up with two subscriptions having passed a guard that was
    correct at the moment each request ran.

    Only `mode="subscription"` sessions are touched. A one-off payment session
    is not a competing obligation and expiring it would cancel a purchase this
    guard has no business cancelling.
    """
    expired = 0
    try:
        sessions = stripe.checkout.Session.list(
            customer=customer_id, status="open", limit=100,
        ).auto_paging_iter()
        for session in sessions:
            if session.get("mode") != "subscription":
                continue
            stripe.checkout.Session.expire(session["id"])
            expired += 1
    except Exception as exc:  # noqa: BLE001 — same rule as the enumeration above
        raise _StripeStateUnavailableError(str(exc)[:200]) from exc
    return expired


# ─── Single-customer promotions and annual billing ───────────────────────────
#
# Stripe applies a `repeating` coupon to every invoice issued inside its
# duration_in_months window, and an annual invoice is ONE invoice for twelve
# months: "100% off for 3 months" on an annual price would be a free year.
#
# That is prevented in Stripe, not here. Annual prices live on their own
# Products, and scripts/stripe_single_customer_promo.py restricts the coupon's
# applies_to to the plan's MONTHLY product, refusing to issue it against a
# product that carries any yearly price. Stripe then declines the discount on
# an annual line wherever it is attempted: Checkout, a plan switch, or the
# Dashboard. Checkout keeps its promotion code box for everyone, so general
# codes such as FOUNDING25 work on monthly and annual exactly as before.
#
# What remains here is detection: an annual subscription carrying one of those
# coupons means the product split was undone, and ops must hear about it.

#: The metadata tag the promotion script stamps on its coupons.
_SINGLE_CUSTOMER_PROMO_TAG = ("bridgeleads_resource", "single_customer_promo")


def _coupon_of(obj: dict) -> dict | None:
    """The coupon behind a discount, as a dict.

    Older API versions embed the coupon object at `coupon`; newer ones move it
    under `source.coupon`, possibly as a bare id, which is retrieved rather than
    guessed at.
    """
    coupon = obj.get("coupon") or (obj.get("source") or {}).get("coupon")
    # An id, or a partial object without metadata, is retrieved: the metadata
    # tag is exactly what the alert needs.
    if isinstance(coupon, str):
        coupon = stripe.Coupon.retrieve(coupon)
    elif isinstance(coupon, dict) and coupon.get("id") and "metadata" not in coupon:
        coupon = stripe.Coupon.retrieve(coupon["id"])
    return coupon or None


def _is_single_customer_promo(coupon: dict | None) -> bool:
    key, value = _SINGLE_CUSTOMER_PROMO_TAG
    return bool(coupon) and (coupon.get("metadata") or {}).get(key) == value


def _subscription_conflict(sub: dict) -> HTTPException:
    """The 409 for a customer who already has a live subscription.

    `incomplete` gets its own message on purpose. It means the first payment
    has not settled yet and Stripe still accepts it for about 23 hours, so the
    customer's actual next step is to finish paying, not to talk to a human.
    Sending them to support for a card that just needs retrying would lock them
    out of their own purchase for a day.

    Everyone else is routed to /billing/change-plan, which modifies the
    subscription they already have. NOT the customer portal: the live portal
    configuration has `subscription_update` DISABLED, so offering it as the
    place to switch plans sends people somewhere that genuinely cannot do it.
    """
    if sub.get("status") == "incomplete":
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "subscription_incomplete",
                "message": (
                    "Your subscription payment is incomplete. Complete that "
                    "payment to continue — starting a new checkout would "
                    "create a second subscription."
                ),
            },
        )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "subscription_exists",
            "message": (
                "You already have a subscription. Change your plan or billing "
                "frequency instead of starting a new one."
            ),
            # The endpoint that CAN do this. Checkout only ever creates, so
            # sending the caller back here would produce the second subscription
            # this refusal exists to prevent.
            "action": "change_plan",
        },
    )


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

    # Validate: resolved price must be a plan price we still SELL. A legacy price
    # is recognised on existing subscriptions only.
    if stripe_price_id not in _PRICE_TO_PLAN or stripe_price_id in _LEGACY_PRICE_TO_PLAN:
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

    # Serialise this user's checkout attempts. Two requests in flight (two tabs,
    # a double-click, a retry after a slow response) would otherwise both read
    # "no subscription", both pass the guard below, and both create a Session.
    # Its own key namespace (4243) so it cannot contend with the entitlement
    # lock at src/api/entitlements.py, which guards a different invariant.
    # Transaction-scoped: released when this request's transaction ends.
    try:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(4243, hashtext(:uid))"),
            {"uid": str(current_user.id)},
        )

        # Re-read the user AFTER taking the lock. `current_user` was loaded
        # before we started waiting, so on the losing side of a race it
        # describes the world as it was before the winner ran — including a
        # stripe_customer_id the winner has just written.
        _row = await db.execute(select(User).where(User.id == current_user.id))
        user = _row.scalar_one()

        # Resolve the customer WITHOUT persisting it yet. The write is deferred
        # to after the Session exists, deliberately: `stripe_customer_id` is the
        # signal the skip-trace meter sweep uses to release held usage, so
        # persisting it on a checkout we are about to REFUSE would fire a
        # customer's held backlog for a subscription that never happened.
        customer_id = user.stripe_customer_id
        newly_resolved = False
        if not customer_id:
            # Adopt this user's existing Stripe customer if there is one —
            # abandoned checkouts and multi-tab starts leave them behind, and
            # creating another would fragment the subscription history the
            # guard below reads.
            customer_id = _resolve_existing_customer(user.email, str(user.id))
            if not customer_id:
                customer_id = stripe.Customer.create(
                    email=user.email,
                    metadata={"user_id": str(user.id)},
                )["id"]
            newly_resolved = True

        # THE GUARD. Asked of Stripe, never of `users.plan`: on this deployment
        # plans are set by hand in the database and no user carries a
        # stripe_subscription_id, so a plan-based check would refuse checkout to
        # every real account we have while still missing an actual duplicate.
        existing = _live_subscription(customer_id)
        if existing is not None:
            _logger.info(
                "checkout refused for user %s: subscription %s is %s",
                user.id, existing.get("id"), existing.get("status"),
            )
            raise _subscription_conflict(existing)

        # Kill any Session already outstanding, then ASK AGAIN. Expiring is not
        # instantaneous and a session can be paid while we are expiring it; the
        # second read is what catches a subscription created in that window.
        _expire_open_checkout_sessions(customer_id)
        existing = _live_subscription(customer_id)
        if existing is not None:
            _logger.warning(
                "checkout refused for user %s: subscription %s (%s) appeared "
                "while expiring outstanding sessions",
                user.id, existing.get("id"), existing.get("status"),
            )
            raise _subscription_conflict(existing)

        # The plan item, plus the metered skip-trace item when one is
        # provisioned for this plan and interval. A metered price must NOT
        # carry a quantity: Stripe rejects the item outright if it does.
        _plan_id, _limit, _interval = _PRICE_TO_PLAN[stripe_price_id]
        line_items: list[dict] = [{"price": stripe_price_id, "quantity": 1}]
        metered_price = _metered_skip_trace_price(_plan_id, _interval)
        if metered_price:
            line_items.append({"price": metered_price})

        session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",

            payment_method_types=["card"],
            # Explicit, and "always" on purpose: a fully discounted first
            # invoice would otherwise let Checkout skip the card, and the first
            # full-price renewal after a promotion would then have nothing to
            # charge.
            payment_method_collection="always",
            line_items=line_items,
            success_url=f"{settings.FRONTEND_URL}/settings?upgrade=success",
            cancel_url=f"{settings.FRONTEND_URL}/settings?upgrade=cancelled",
            metadata={"user_id": current_user.id, "price_id": price_or_product_id},
            # Every paid session, monthly and annual. Stripe validates each code:
            # its customer restriction, redemption count, expiry and, through
            # the coupon's applies_to, which products it may discount (see the
            # single-customer promotion note above).
            allow_promotion_codes=True,
        )

        # Defense in depth for the one window the checks above leave: a Session
        # that was already being paid while we expired it. Expiring a Session
        # mid-completion raises (fail closed), and one that completed just
        # before is visible now. Either way the new Session must not survive.
        try:
            late = _live_subscription(customer_id)
        except _StripeStateUnavailableError:
            # We cannot tell whether this Session is now a second subscription in
            # waiting, so it must not be left open for the caller to pay.
            try:
                stripe.checkout.Session.expire(session["id"])
            except Exception as exc:  # noqa: BLE001 - already failing closed
                _logger.error(
                    "checkout: could not expire session %s after a failed re-check "
                    "for user %s (%s)", session["id"], user.id, str(exc)[:200],
                )
            raise
        if late is not None:
            try:
                stripe.checkout.Session.expire(session["id"])
            except Exception as exc:  # noqa: BLE001 - an unexpirable Session is "unknown"
                raise _StripeStateUnavailableError(str(exc)[:200]) from exc
            _logger.warning(
                "checkout refused for user %s: subscription %s (%s) appeared "
                "after the new session was created; session expired",
                user.id, late.get("id"), late.get("status"),
            )
            raise _subscription_conflict(late)

        # Persist the customer id only now that a Session exists. Before the
        # guard this write happened first, which meant a REFUSED checkout still
        # published the id that releases held skip-trace meter events.
        if newly_resolved:
            user.stripe_customer_id = customer_id
            await db.flush()

        return {"checkout_url": session.url}
    except HTTPException:
        raise
    except _StripeStateUnavailableError as exc:
        # We could not establish whether this customer already has a
        # subscription. Failing CLOSED: creating a Session here is the one
        # outcome that can charge someone twice, and "try again" costs a
        # customer seconds where a duplicate subscription costs them money and
        # us a refund.
        _logger.error(
            "checkout: could not read Stripe subscription state for user %s "
            "(%s) — refusing rather than risking a duplicate subscription",
            current_user.id, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing is temporarily unavailable. Please try again later.",
        ) from exc
    except Exception:
        _logger.exception("Checkout failed for user %s", current_user.id)
        raise HTTPException(status_code=502, detail="Checkout temporarily unavailable")


# ─── Plan switching: modify the subscription, never create a second ───────────


class _UnrecognisedSubscriptionError(Exception):
    """The live subscription does not carry a price this deployment sells."""


def _plan_change_billing(current_price: str | None, new_price: str) -> dict:
    """How Stripe bills a plan change, decided by direction (audit 2026-09-25, N-02).

    The app grants a bigger tier the moment Stripe says so and defers a smaller
    one to the quota boundary (billing_entitlement.apply_plan_change). Billing has
    to agree with both halves, or the gap between them is for sale:

    - Upgrade, or a same-tier interval move: invoice the proration NOW and pay
      it before the subscription changes. ``error_if_incomplete`` makes Stripe
      refuse the whole update when the payment fails or needs authentication,
      so an unpaid upgrade never reaches the webhook that grants it.
    - Downgrade: no proration credit. The customer keeps the bigger tier until
      the boundary, which they already paid for, and gets nothing back for time
      the app still honours.
    """
    current = _PRICE_TO_PLAN.get(current_price or "")
    new = _PRICE_TO_PLAN[new_price]
    downgrade = current is not None and _rank(new[1]) < _rank(current[1])
    return {
        "proration_behavior": "none" if downgrade else "always_invoice",
        "payment_behavior": "error_if_incomplete",
    }


def _plan_change_items(sub: dict, new_price: str, new_plan: str, new_interval: str) -> list[dict]:
    """The `items` array that turns `sub` into the requested plan. Pure.

    ONE array, ONE Subscription.modify call. Sequential calls would leave the
    subscription briefly holding a monthly metered price beside an annual plan
    price, and Stripe requires every item on a subscription to share a recurring
    interval — the second call would be rejected and the customer left stranded
    mid-transition, which is a worse state than either end of it.

    The metered item is REPLACED (delete + add), never re-priced in place, on any
    change of price. Updating an item keeps its id and its `created`, and
    assert_billable uses exactly that field to refuse billing usage at a rate
    agreed after the usage happened. Re-pricing in place would leave an item that
    looks like it had always carried the new rate, so this period's earlier
    lookups would be charged at it. Replacing gives the new item a new `created`,
    so pre-switch usage fails that check and goes to needs_review instead.

    That is a deliberate trade, not an oversight: a review-queue row costs a
    conversation, a silent retroactive re-rate costs a customer money they never
    agreed to pay.
    """
    items: list[dict] = []
    # Every metered price this deployment knows about, derived from the same map
    # _metered_skip_trace_price selects from. Read from that rather than
    # importing the worker's copy so the item this identifies and the item that
    # gets attached can never come from two different sources of truth.
    metered_ids = {
        pid
        for by_interval in _SKIP_TRACE_METERED_PRICE.values()
        for pid in by_interval.values()
        if pid and pid.startswith("price_")
    }

    licensed: list = []
    metered: list = []
    unknown: list = []
    for item in (sub.get("items") or {}).get("data") or []:
        pid = ((item or {}).get("price") or {}).get("id")
        if pid in _PRICE_TO_PLAN:
            licensed.append(item)
        elif pid in metered_ids:
            metered.append(item)
        else:
            unknown.append(pid)

    # The shape is VALIDATED, not sampled. The first version of this loop kept
    # the LAST match of each kind while _plan_item_price_id reads the FIRST, so a
    # subscription carrying two licensed items would have had only one re-priced
    # and the other left attached — two plan charges after a same-interval
    # switch, or a stranded monthly item that makes an annual switch fail the
    # interval rule. Codex reproduced it. Anything we cannot describe exactly is
    # refused for a human to reconcile, because every way of proceeding here
    # leaves the customer on a subscription nobody chose.
    if len(licensed) != 1:
        raise _UnrecognisedSubscriptionError(
            f"expected exactly one plan price, found {len(licensed)}"
        )
    if len(metered) > 1:
        raise _UnrecognisedSubscriptionError(
            f"expected at most one metered price, found {len(metered)}"
        )
    if unknown:
        raise _UnrecognisedSubscriptionError(
            f"subscription carries {len(unknown)} price(s) this deployment does "
            "not recognise"
        )

    licensed_item = licensed[0]
    metered_item = metered[0] if metered else None

    items.append({"id": licensed_item["id"], "price": new_price})

    target_metered = _metered_skip_trace_price(new_plan, new_interval)
    current_metered = ((metered_item or {}).get("price") or {}).get("id")

    if current_metered != target_metered:
        if metered_item is not None:
            items.append({"id": metered_item["id"], "deleted": True})
        if target_metered:
            items.append({"price": target_metered})

    return items


# The sync pool this borrows from is pool_size=2 / max_overflow=3, so more than
# five concurrent plan changes would queue on it and surface a 30s timeout as an
# unhandled 500 (Codex). Bounded here instead, well under that, so the failure
# mode is a clean 503 that says "try again" rather than a stack trace.
#
# Created lazily, per running loop, rather than at import. A module-level
# Semaphore binds to whichever loop first contends on it, and a process that
# runs more than one loop over its lifetime — test clients, a loop restart —
# then raises RuntimeError on a later acquire.
_stranded_slots_by_loop: dict = {}


def _stranded_mark_slots() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _stranded_slots_by_loop.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(3)
        _stranded_slots_by_loop[loop] = sem
    return sem


def _mark_stranded_metered_usage(user_id: str, period_start: int) -> int:
    """Move this period's REPORTED meter events to needs_review. Own transaction.

    Runs on an independent session, and that is not incidental. The obvious
    thing — issuing this on the request's session and committing — would commit
    the REQUEST's transaction, and the checkout/plan-change guard is held by
    `pg_advisory_xact_lock`, which is transaction-scoped. Committing to make the
    marking durable would therefore release the lock while the plan change is
    still in flight and let a concurrent checkout through the guard, trading a
    lost-revenue bug for a duplicate-subscription one.

    So it gets its own connection: durable on its own, and the caller's
    transaction (and its lock) are untouched. `system_sync_session` because this
    is deliberately a system-level write; the tenant is pinned explicitly in the
    predicate below rather than by an RLS GUC.

    Off the event loop via asyncio.to_thread — it is a sync session inside an
    async route, and a blocking call there stalls every other request.
    """
    from src.db.session import system_sync_session

    with system_sync_session() as db:
        # A REAL bound, on the database, which can stop the statement. This runs
        # while the caller holds the plan-change advisory lock, so a statement
        # that blocks here blocks that user's plan change; better to fail it and
        # let the caller 503 than to sit on the connection.
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        db.execute(text("SET LOCAL statement_timeout = '15s'"))
        rows = db.execute(
            text("""
                UPDATE skip_trace_meter_events
                   SET disposition = 'needs_review',
                       disposition_at = NOW(),
                       disposition_reason = 'metered_item_replaced_before_invoice'
                 WHERE user_id = CAST(:uid AS uuid)
                   AND disposition = 'reported'
                   AND (
                         (usage_at IS NOT NULL
                          AND usage_at >= to_timestamp(:period_start))
                         -- Rows written before migration 093 carry no usage_at
                         -- at all. They were still reported and are still
                         -- stranded by the delete, so they are placed by the
                         -- outbox write time instead: it is at or after the
                         -- usage, which over-includes rather than misses one.
                      OR (usage_at IS NULL
                          AND created_at >= to_timestamp(:period_start))
                   )
                RETURNING id
            """),
            {"uid": user_id, "period_start": period_start},
        ).fetchall()
        db.commit()
        return len(rows)


def _no_subscription_conflict() -> HTTPException:
    """409 for a plan change on an account that has no subscription yet."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "no_subscription",
            "message": (
                "You do not have a subscription to change yet. Choose a plan to "
                "get started."
            ),
        },
    )


class ChangePlanRequest(BaseModel):
    price_id: str


@router.post("/change-plan")
async def change_plan(
    request: Request,
    body: ChangePlanRequest,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> dict:
    """Move an EXISTING subscriber to a different plan or billing interval.

    Checkout creates a subscription; this changes the one that already exists.
    They are separate endpoints because they are separate operations, and
    conflating them is what produced the defect: create_checkout always built a
    subscription-mode Session and nothing modified or cancelled the old one, so a
    monthly subscriber buying annual ended up holding BOTH — two live
    obligations, and with the metered skip-trace item attached, two metered items
    on one meter for one customer.

    Nothing in here can create a subscription. If there is no live one to modify
    this returns 409 and points at checkout, which is the endpoint that may.
    """
    await rate_limit(request, zone="stripe", identifier=current_user.id)

    _PRODUCT_TO_PRICE = {
        settings.STRIPE_PRODUCT_PRO: settings.STRIPE_PRICE_PRO,
        settings.STRIPE_PRODUCT_BUSINESS: settings.STRIPE_PRICE_BUSINESS,
        settings.STRIPE_PRODUCT_AGENCY: settings.STRIPE_PRICE_AGENCY,
    }
    stripe_price_id = _PRODUCT_TO_PRICE.get(body.price_id, body.price_id)

    if stripe_price_id not in _PRICE_TO_PLAN or stripe_price_id in _LEGACY_PRICE_TO_PLAN:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid plan")
    if not stripe_price_id.startswith("price_"):
        _logger.error(
            "change-plan: resolved id %r is not a price id — STRIPE_PRICE_* is "
            "misconfigured (check Railway env on api AND worker).",
            stripe_price_id,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing is temporarily unavailable. Please try again later.",
        )

    new_plan, _limit, new_interval = _PRICE_TO_PLAN[stripe_price_id]

    try:
        # The SAME lock namespace as checkout (4243). Between them these two
        # guard ONE invariant — this customer has exactly one subscription — so
        # they must not run at once. A separate key here would let a checkout and
        # a plan change interleave and produce the second subscription that both
        # of them exist to prevent.
        await db.execute(
            text("SELECT pg_advisory_xact_lock(4243, hashtext(:uid))"),
            {"uid": str(current_user.id)},
        )

        _row = await db.execute(select(User).where(User.id == current_user.id))
        user = _row.scalar_one()

        customer_id = user.stripe_customer_id
        if not customer_id:
            # No customer means no subscription. Deliberately NOT creating one:
            # the purpose of this endpoint is to avoid a second subscription, and
            # a customer minted here would be one more thing for the checkout
            # guard to have to enumerate correctly.
            raise _no_subscription_conflict()

        # Read AFTER the lock. A checkout that won the race may have just created
        # the subscription we are about to modify, and a pre-lock read misses it.
        sub = _live_subscription(customer_id)
        if sub is None:
            raise _no_subscription_conflict()

        if sub.get("status") in ("past_due", "unpaid"):
            # create_prorations credits the UNUSED portion of the current period
            # — but only "unused" is checked, never "paid". On a subscription
            # whose latest invoice has not settled, that issues a credit against
            # money we never received, and Stripe documents exactly this risk.
            # The customer's real next step is the outstanding invoice.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "subscription_unpaid",
                    "message": (
                        "There is an unpaid invoice on your subscription. Settle "
                        "that first and you will be able to change your plan."
                    ),
                },
            )

        if sub.get("status") == "incomplete":
            # The first payment has not settled and Stripe still accepts it for
            # about 23 hours. Modifying the subscription underneath an in-flight
            # payment changes what the customer is being charged mid-transaction.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "subscription_incomplete",
                    "message": (
                        "Your subscription payment is incomplete. Complete that "
                        "payment first, then change your plan."
                    ),
                },
            )

        # Shape FIRST, then the same-plan shortcut. The other way round, a
        # subscription carrying two licensed items or an unrecognised price
        # answered "you are already on that plan" and nobody ever heard about
        # it, because the only thing that inspects the shape is
        # _plan_change_items and the shortcut returned before reaching it
        # (Codex). A malformed subscription is worth saying out loud even when
        # the caller is asking for nothing.
        items = _plan_change_items(sub, stripe_price_id, new_plan, new_interval)

        current_price = _plan_item_price_id((sub.get("items") or {}).get("data") or [])
        if current_price == stripe_price_id:
            # Not an error, and deliberately not a Stripe call: modifying a
            # subscription to what it already is can still write prorations.
            return {
                "status": "unchanged",
                "plan": new_plan,
                "message": "You are already on that plan.",
            }

        current_interval = (_PRICE_TO_PLAN.get(current_price) or (None, None, None))[2]
        interval_changed = current_interval != new_interval

        modify_kwargs: dict = {
            "items": items,
            # Upgrades are paid before Stripe applies them; downgrades are not
            # credited. See _plan_change_billing for why (N-02).
            **_plan_change_billing(current_price, stripe_price_id),
            "metadata": {"user_id": str(user.id), "price_id": body.price_id},
        }
        if interval_changed:
            # A monthly-to-annual move cannot leave the old period running: the
            # items recur yearly now, and the period they were billed in was
            # monthly. Restarting the cycle makes that boundary explicit rather
            # than leaving Stripe to infer one, and it is the boundary the
            # skip-trace entitlement window keys off.
            modify_kwargs["billing_cycle_anchor"] = "now"

        # Usage already REPORTED against a metered item we are about to delete
        # will never be invoiced: Stripe does not carry a deleted subscription
        # item's usage onto the invoice. Our rows still say `reported`, which
        # reads as "on its way to an invoice", so without this the money is
        # silently uncollectable with nothing pointing at it.
        #
        # Deleting is still right — keeping the item re-rates this period's
        # earlier lookups at the new price — so the usage is HANDED OVER rather
        # than abandoned: needs_review, where the ops alert names it and the
        # settle script recovers it on an invoice.
        #
        # BEFORE the Stripe call, and that ordering is the whole point. Written
        # afterwards it was not retry-safe: if this UPDATE or the request's
        # commit failed after Stripe had already changed the subscription, the
        # rows stayed `reported` and a retry took the same-plan shortcut and
        # never came back to them (Codex). Marking first can only over-flag —
        # if Stripe then refuses, some rows sit in review that did not need to,
        # and a human releases them. That is the survivable direction; the other
        # one loses revenue with no record that it existed.
        if any(i.get("deleted") for i in items):
            period_start = sub.get("current_period_start")
            if period_start is not None:
                try:
                    # No asyncio.timeout here, deliberately. It looked like a
                    # bound and was not: to_thread cannot be cancelled, so a
                    # timeout would stop us AWAITING the thread while the thread
                    # kept running and committed anyway — after we had already
                    # returned 503 — and would release the semaphore slot while
                    # still holding the connection (Codex reproduced it). The
                    # real bound is inside the session, on the database, where
                    # it can actually stop the statement.
                    async with _stranded_mark_slots():
                        n_stranded = await asyncio.to_thread(
                            _mark_stranded_metered_usage,
                            str(user.id), int(period_start),
                        )
                except Exception as exc:  # noqa: BLE001
                    # This runs BEFORE Stripe, so failing here has changed
                    # nothing: no subscription was modified and no usage was
                    # marked. Refusing is therefore free, and proceeding is not
                    # — the whole reason the marking comes first is that a plan
                    # change without it strands revenue silently.
                    _logger.error(
                        "change-plan: could not record stranded skip-trace usage "
                        "for user %s (%s) — refusing the plan change rather than "
                        "making it without the record",
                        user.id, str(exc)[:200],
                    )
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Billing is temporarily unavailable. Please try again later.",
                    ) from exc
                if n_stranded:
                    _logger.warning(
                        "change-plan: user %s has %d reported skip-trace meter "
                        "event(s) against the metered item about to be replaced. "
                        "Stripe will not invoice them; moved to needs_review for "
                        "manual recovery BEFORE the subscription is modified.",
                        user.id, n_stranded,
                    )

        try:
            updated = stripe.Subscription.modify(sub["id"], **modify_kwargs)
        except stripe.error.CardError as exc:
            # error_if_incomplete: the payment failed or needs the customer to
            # authenticate, and Stripe left the subscription exactly as it was.
            _logger.info(
                "change-plan: payment for %s -> %s not completed for user %s (%s)",
                current_price, stripe_price_id, user.id, getattr(exc, "code", None),
            )
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "code": "plan_change_payment_failed",
                    "message": (
                        "Your plan was not changed because the payment did not go "
                        "through. Update your card in Manage billing, then try again."
                    ),
                },
            ) from exc
        except Exception as exc:  # noqa: BLE001 — surfaced, never swallowed
            _logger.exception(
                "change-plan: Stripe refused to modify subscription %s for user %s "
                "(%s -> %s)", sub.get("id"), user.id, current_price, stripe_price_id,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Could not change your plan. Please try again or contact support.",
            ) from exc

        _logger.info(
            "change-plan: user %s %s -> %s on subscription %s (interval_changed=%s)",
            user.id, current_price, stripe_price_id, sub["id"], interval_changed,
        )

        # users.plan is NOT written here. customer.subscription.updated is the
        # single writer for it, so the plan the app enforces always reflects what
        # Stripe actually did rather than what we asked it to do. If the modify
        # half-succeeds, the app stays on the plan Stripe still says is in force.
        return {
            "status": "updated",
            "plan": new_plan,
            "subscription_id": (updated or {}).get("id") or sub["id"],
            "interval_changed": interval_changed,
        }
    except HTTPException:
        raise
    except _UnrecognisedSubscriptionError as exc:
        _logger.error(
            "change-plan: subscription for user %s carries no recognised plan "
            "price (%s) — refusing to guess which item to re-price",
            current_user.id, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "subscription_unrecognised",
                "message": (
                    "Your subscription could not be matched to a plan. Please "
                    "contact support."
                ),
            },
        ) from exc
    except _StripeStateUnavailableError as exc:
        _logger.error(
            "change-plan: could not read Stripe subscription state for user %s "
            "(%s) — refusing rather than acting on an unknown subscription",
            current_user.id, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing is temporarily unavailable. Please try again later.",
        ) from exc


# ─── Customer portal ──────────────────────────────────────────────────────────

@router.post("/portal")
async def customer_portal(request: Request, current_user: CurrentUser) -> dict:
    """Return a Stripe Customer Portal URL for managing subscriptions.

    The portal here is payment method, invoices and cancel. Plan CHANGES go
    through create_checkout, which is what the plan cards call.

    That distinction now matters. Subscriptions carry a usage-based skip-trace
    item, and Stripe restricts the portal's plan-switch flow for subscriptions
    that have one. This deployment is unaffected: the live portal configuration
    (bpc_1TGRdU..., the default) has subscription_update DISABLED, verified
    against the account, so the portal never offered a plan switch. If someone
    turns "Switch plan" on in the Dashboard, check it against a subscription
    that actually has the metered item before trusting it (Codex raised this).
    """
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
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    stripe_signature: str = Header(..., alias="stripe-signature"),
) -> dict:
    """Handle Stripe webhook events to keep plan state in sync.

    Registers for:
      - checkout.session.completed      → activate new plan (trial → paid)
      - customer.subscription.created   → same as updated, entitled statuses only
      - customer.subscription.updated   → upgrades / downgrades / cancellation
      - customer.subscription.deleted   → downgrade to starter
      - invoice.payment_failed          → start the dunning grace + notify

    Recovery from dunning arrives as customer.subscription.updated (past_due ->
    active); invoice.payment_succeeded is deliberately not handled.

    NOTE what these handlers deliberately do NOT do: advance a quota window.
    Record quota rolls on the user's entitlement anniversary, lazily, inside the
    statement that next charges them (and hourly via reconcile_quota_periods).
    Making a webhook the mechanism would strand a renewed payer at cap behind a
    late delivery and hand out a second bucket on a replay — Stripe retries for
    three days and can deliver out of order. Webhooks update plan, limit, status
    and lifecycle dates; the window advances on its own.
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

    # Idempotency (C4, full-SaaS review), durable and transactional.
    #
    # The event is recorded in stripe_webhook_events in the SAME transaction as
    # the changes its handler makes, so "recorded" and "applied" commit or roll
    # back together. It used to be a Redis key written BEFORE the handler ran,
    # which meant a handler that raised (a Stripe read blip), a failed commit, or
    # a process killed mid-request (every deploy restarts the api) left an event
    # Stripe would retry and we would skip. For a fully discounted checkout that
    # event is the only one that activates the plan.
    #
    # The transaction-scoped advisory lock serialises deliveries of ONE event: a
    # duplicate waits for the first attempt to commit (then sees the row and
    # acknowledges) or roll back (then processes it itself). A killed process
    # drops its connection and with it the lock. lock_timeout bounds that wait,
    # and a delivery that times out gets a 409 so Stripe retries it later.
    event_id = event.get("id") or ""
    try:
        if event_id:
            # The timeout bounds ONLY the wait for this event's lock. It is reset
            # straight after, so the user-row locks the handlers take keep their
            # normal behaviour instead of turning ordinary contention into 409s.
            await db.execute(text(f"SET LOCAL lock_timeout = '{_WEBHOOK_LOCK_TIMEOUT}'"))
            await db.execute(
                text("SELECT pg_advisory_xact_lock(4244, hashtext(:eid))"),
                {"eid": event_id},
            )
            await db.execute(text("SET LOCAL lock_timeout TO DEFAULT"))
            already = await db.execute(
                text("SELECT 1 FROM stripe_webhook_events WHERE event_id = :eid"),
                {"eid": event_id},
            )
            if already.first() is not None:
                _logger.info("stripe webhook dedup: already processed %s", event_id)
                return {"received": True}

        notifications = await _dispatch_stripe_event(
            event["type"], event["data"]["object"], db
        )

        if event_id:
            await _record_stripe_event(db, event_id, event["type"])
        await db.commit()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
            raise
        await db.rollback()
        _logger.info("stripe webhook: %s is being processed elsewhere; retry later", event_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Event is already being processed",
        ) from exc

    # Only once the changes and the ledger row are committed, and after the
    # response is sent. Sent inside the transaction, a commit that then failed
    # left an email Stripe's retry would send again; sent inline, a slow email
    # provider held the webhook open (and, before, the user's row lock).
    for notify in notifications:
        background_tasks.add_task(_run_notification, notify)
    return {"received": True}


def _run_notification(notify: Callable[[], None]) -> None:
    """Run one post-commit notification; a failure is logged, never raised.

    Background tasks run in sequence and Starlette stops at the first one that
    raises, so an email outage must not also swallow the in-app notice.
    """
    try:
        notify()
    except Exception as exc:  # noqa: BLE001 - the billing change is already committed
        _logger.warning(
            "stripe webhook: post-commit notification failed (%s: %s)",
            type(exc).__name__, str(exc)[:200],
        )


async def _record_stripe_event(db: AsyncSession, event_id: str, event_type: object) -> None:
    await db.execute(
        text(
            "INSERT INTO stripe_webhook_events (event_id, event_type) "
            "VALUES (:eid, :etype)"
        ),
        {"eid": event_id, "etype": str(event_type)[:100]},
    )


#: Upper bound on waiting for another delivery of the same event to finish.
_WEBHOOK_LOCK_TIMEOUT = "15s"
#: SQLSTATE for lock_timeout expiry.
_LOCK_NOT_AVAILABLE = "55P03"

_ENTITLED_CHECKOUT_STATUSES = ("active", "trialing")
_DUNNING_SUBSCRIPTION_STATUSES = ("past_due", "unpaid")


async def _dispatch_stripe_event(
    event_type: str, data: dict, db: AsyncSession
) -> list[Callable[[], None]]:
    """Apply one event. Returns the notifications to send once it is committed."""
    if event_type == "checkout.session.completed":
        await _handle_checkout_completed(data, db)

    elif event_type == "customer.subscription.created":
        # Normally checkout.session.completed gets there first. This is the belt
        # for when it does not: a fully discounted subscription is created
        # active, so no later update is guaranteed to follow. Entitled statuses
        # only, and checked on the RE-READ subscription: through the update path
        # an `incomplete` Agency subscription would otherwise count as an
        # upgrade and grant Agency before anything was paid.
        await _handle_subscription_updated(data, db, require_entitled=True)

    elif event_type == "customer.subscription.updated":
        await _handle_subscription_updated(data, db)

    elif event_type == "customer.subscription.deleted":
        await _handle_subscription_deleted(data, db)

    elif event_type == "invoice.payment_failed":
        return await _handle_payment_failed(data, db)

    return []


# ─── Webhook handlers ─────────────────────────────────────────────────────────

def _stripe_ts(value: object) -> datetime | None:
    """Stripe unix seconds -> an aware UTC datetime, or None.

    Stripe sends every timestamp as an integer epoch. Building a NAIVE datetime
    from one (``datetime.fromtimestamp`` without a tz) would interpret it in the
    server's local zone, which on a Railway box happens to be UTC and on a
    developer's box does not — a boundary that is silently right in production
    and silently wrong in testing is worse than one that is always wrong.
    Tolerates None/garbage so a Stripe field we do not receive is simply absent
    rather than a 500 on a webhook Stripe would then retry for three days.
    """
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError, OverflowError):
        _logger.warning("stripe timestamp not parseable: %r", value)
        return None


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
    price_id = _plan_item_price_id(subscription["items"]["data"])
    plan_info = _PRICE_TO_PLAN.get(price_id) if price_id else None

    if not plan_info:
        # Paid checkout but the price isn't in our plan map — entitlement would be
        # silently lost. Alert with recovery ids; do NOT grant a fallback plan.
        _alert_billing_gap(
            "checkout.session.completed price not in plan map: user PAID but "
            "plan NOT activated",
            f"unmapped-price:{price_id}",
            event=data.get("id"),
            price_id=price_id,
            customer=session_customer_id,
            subscription=subscription_id,
            user_id=user_id,
        )
        return

    plan_name, records_limit, _interval = plan_info
    # FOR UPDATE. checkout.session.completed and customer.subscription.updated
    # are two DIFFERENT Stripe events describing ONE conversion, so the route's
    # per-event ledger dedup does not stop them running concurrently on two API
    # workers. Both would load a user with first_paid_at NULL, both would decide
    # this is a fresh entitlement, and both would zero the counter — a free
    # bucket, and worse, a stale second commit can wipe usage consumed between
    # them. Locking serialises them: the loser blocks, re-reads the newer row
    # version under READ COMMITTED, sees first_paid_at set and does nothing.
    # Users-only lock, so the jobs -> users order is untouched. (Codex)
    result = await db.execute(
        select(User).where(User.id == user_id).with_for_update()
    )
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

    # A completed Session is not proof of an entitled subscription. Checkout
    # never reads payment_status or a PaymentIntent here, which is what lets a
    # fully discounted session (no_payment_required, no PaymentIntent) activate
    # normally, so the subscription's own status is the gate. Anything not yet
    # entitled is left to customer.subscription.updated, which converts it the
    # moment Stripe says it is active. The customer binding above still stands,
    # because that update is looked up by it.
    if subscription.get("status") not in _ENTITLED_CHECKOUT_STATUSES:
        _logger.warning(
            "checkout.session.completed: subscription %s for user %s is %s; "
            "plan not activated yet, waiting for the subscription to become "
            "active", subscription_id, user_id, subscription.get("status"),
        )
        await db.flush()
        return

    # The checkout guard refuses a second subscription, but it cannot see a
    # Session that was already open and paid in the gap. If the subscription we
    # have on record is a different one that is still live, the customer now
    # owes on two: surface it for a human. Never cancel from here, because which
    # one the customer meant to keep is not knowable in a webhook.
    previous_id = user.stripe_subscription_id
    if previous_id and previous_id != subscription_id:
        try:
            previous_status = stripe.Subscription.retrieve(previous_id).get("status")
        except Exception as exc:  # noqa: BLE001 - an alert must never fail the webhook
            previous_status = None
            _logger.warning(
                "checkout.session.completed: could not read previous subscription "
                "%s for user %s (%s)", previous_id, user_id, str(exc)[:200],
            )
        if previous_status and previous_status not in _TERMINAL_SUBSCRIPTION_STATUSES:
            _alert_billing_gap(
                "checkout completed while another subscription is still live: "
                "customer may be billed twice",
                f"second-live-subscription:{user_id}",
                previous_subscription=previous_id,
                previous_status=previous_status,
                subscription=subscription_id,
                customer=session_customer_id,
                user_id=user_id,
            )

    # P1 (trial -> paid) / P9 (resubscribe). Durable entitlement (migration 077)
    # plus the entitlement window (migration 088): a customer who consumed their
    # trial allowance and then PAID starts a fresh paid month AT CONVERSION,
    # instead of receiving nothing until the calendar 1st. The reset is gated on
    # first_paid_at / paid_entitlement_ended_at, not on this event, so a Stripe
    # replay — or a second event describing the same conversion — cannot zero
    # the counter twice. subscription["status"] is authoritative (retrieved
    # above, so it reflects Stripe NOW rather than whenever the event was
    # queued).
    activate_paid_plan(
        user,
        plan=plan_name,
        records_limit=records_limit,
        subscription_id=subscription_id,
        status=subscription.get("status"),
        billing_cycle_anchor=_stripe_ts(subscription.get("billing_cycle_anchor")),
    )
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


async def _handle_subscription_updated(
    data: dict, db: AsyncSession, *, require_entitled: bool = False
) -> None:
    """Handle plan changes (upgrades or downgrades) and scheduled cancellation.

    ``require_entitled`` is for ``customer.subscription.created``: it applies
    nothing unless the subscription is active or trialing, judged on the re-read
    subscription when Stripe answers.

    RE-READS the subscription from Stripe rather than trusting the event body.
    Stripe retries for three days and does not guarantee ordering, so a delayed
    ``updated`` describing a plan the customer has since changed again would
    otherwise overwrite newer state — silently restoring a cancelled downgrade
    or an old cap. Retrieving makes the LAST handler to run write the CURRENT
    truth, whatever order the events arrived in, and needs no extra column to
    track event times. If Stripe is unreachable we fall back to the event body
    (the previous behaviour) rather than dropping a real plan change.
    """
    customer_id = data.get("customer")
    if not customer_id:
        return

    # FOR UPDATE — see _handle_checkout_completed: this handler can also perform
    # the one-time conversion reset, so it must serialise against the checkout
    # handler for the same user. Taken BEFORE the re-read below: locked after
    # it, a slow read of an older state could win the lock second and overwrite
    # the newer state another delivery had just written. Locked first, the
    # handler that writes last is also the one that asked Stripe last. (Codex)
    result = await db.execute(
        select(User).where(User.stripe_customer_id == customer_id).with_for_update()
    )
    user = result.scalar_one_or_none()

    subscription_id = data.get("id")
    if subscription_id:
        try:
            data = dict(
                stripe.Subscription.retrieve(
                    subscription_id, expand=["items.data.price", "discounts"]
                )
            )
        except Exception as exc:  # noqa: BLE001 — never 500 a webhook
            # BOTH paths now fail closed. A creation was already only acted on
            # when Stripe confirms it is entitled NOW, because the event body
            # can be stale (created active, cancelled since).
            #
            # `updated` used to fall through and APPLY the event payload here.
            # That was the gap: the payload is a snapshot from when the event was
            # emitted, and Stripe delivers out of order and retries for days. So
            # during a Stripe API outage a validly-signed OLDER event could
            # overwrite newer state — reinstating a plan the customer cancelled,
            # or clearing dunning by writing a stale `active`. The row lock above
            # does not help: it serializes writers, it does not order them.
            #
            # Raising instead hands the problem back to Stripe's retry, which is
            # strictly safer than committing an unverifiable snapshot. The
            # subscription state we already hold stays untouched until we can
            # actually read the authoritative value. (Codex-reviewed.)
            _logger.error(
                "customer.subscription.%s: could not re-read %s from Stripe "
                "(%s); refusing to apply the possibly-stale event body — "
                "retrying later",
                "created" if require_entitled else "updated",
                subscription_id, str(exc)[:200],
            )
            raise

    if require_entitled and data.get("status") not in _ENTITLED_CHECKOUT_STATUSES:
        _logger.info(
            "customer.subscription.created: %s is %s; nothing granted until it "
            "is active", subscription_id, data.get("status"),
        )
        return

    items = (data.get("items") or {}).get("data", [])
    if not items:
        return

    price_id = _plan_item_price_id(items)
    plan_info = _PRICE_TO_PLAN.get(price_id) if price_id else None

    if not plan_info:
        # Subscription changed to a price we don't map — the plan change would be
        # silently lost. Alert with recovery ids; do NOT guess a plan.
        _alert_billing_gap(
            "customer.subscription.updated price not in plan map: plan change "
            "NOT applied",
            f"unmapped-price:{price_id}",
            price_id=price_id,
            customer=customer_id,
        )
        return

    plan_name, records_limit, _interval = plan_info

    # A single-customer promotion coupon on an ANNUAL subscription means the
    # product split that keeps it monthly-only was undone (see the note above
    # _coupon_of). An alert only: the entitlement is still what the price says.
    if _interval == "year":
        for discount in data.get("discounts") or []:
            if not isinstance(discount, dict):
                continue
            try:
                promo = _is_single_customer_promo(_coupon_of(discount))
            except Exception as exc:  # noqa: BLE001 - an alert must never fail the webhook
                _logger.warning(
                    "subscription %s: could not read discount %s (%s)",
                    subscription_id, discount.get("id"), str(exc)[:200],
                )
                continue
            if promo:
                _alert_billing_gap(
                    "annual subscription carries a single-customer promotion coupon: "
                    "the whole year may be discounted",
                    f"annual-single-customer-promo:{subscription_id}",
                    subscription=subscription_id,
                    discount=discount.get("id"),
                    customer=customer_id,
                )
                break

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

    # A subscription we have not recorded may only CHANGE entitlement once it is
    # entitled. Through apply_plan_change an `incomplete` Agency subscription
    # ranks above a trial's 1,000 records and would be applied as an upgrade
    # before anything was paid. The recorded subscription keeps every status
    # transition (past_due, unpaid, ...), which is what dunning depends on, and
    # so does an account already paying for THIS plan without a recorded id
    # (plans set by hand before subscription ids were stored): a status change
    # on the plan they already hold grants nothing, and blocking it would stop
    # their dunning from ever starting. (Codex)
    # Dunning statuses only: an unrelated `incomplete` subscription on the same
    # plan must not be adopted as the account's subscription, or its later
    # payment failure or expiry would freeze or downgrade a paying customer.
    dunning_on_plan_already_paid_for = (
        data.get("status") in _DUNNING_SUBSCRIPTION_STATUSES
        and user.stripe_subscription_id is None
        and user.first_paid_at is not None
        and normalize_plan(user.plan) == normalize_plan(plan_name)
    )
    if (
        dunning_on_plan_already_paid_for
        and data.get("id") != user.stripe_subscription_id
    ):
        # Nothing on record says WHICH subscription is theirs, so bind this one
        # only when it is unambiguous: the customer's sole live subscription.
        # With two, starting dunning could freeze a customer over the wrong
        # one, so a human decides. Failing to ask raises and Stripe retries.
        live_ids = [
            s.get("id") for s in stripe.Subscription.list(
                customer=customer_id, status="all", limit=100,
            ).auto_paging_iter()
            if s.get("status") not in _TERMINAL_SUBSCRIPTION_STATUSES
        ]
        if live_ids != [data.get("id")]:
            _alert_billing_gap(
                "subscription entered dunning for an account with no recorded "
                "subscription and more than one live subscription: not applied",
                f"ambiguous-dunning:{user.id}",
                subscription=data.get("id"),
                live_subscriptions=",".join(str(i) for i in live_ids),
                customer=customer_id,
                user_id=user.id,
            )
            return

    if (
        data.get("id") != user.stripe_subscription_id
        and data.get("status") not in _ENTITLED_CHECKOUT_STATUSES
        and not dunning_on_plan_already_paid_for
    ):
        _logger.info(
            "customer.subscription.updated: %s for user %s is %s and not the "
            "recorded subscription; nothing applied until it is active",
            data.get("id"), user.id, data.get("status"),
        )
        return

    # P4 / P5 / P6a: upgrades take effect now and keep both the window and the
    # counter; downgrades are parked and applied at the next entitlement
    # boundary so nobody is cut to a smaller cap on quota they already paid for;
    # a scheduled cancellation records when paid access stops. The durable
    # entitlement state (migration 077) is kept in sync throughout — an entitled
    # status ends the app-side trial so expire_trials won't downgrade a payer.
    outcome = apply_plan_change(
        user,
        plan=plan_name,
        records_limit=records_limit,
        subscription_id=data.get("id"),
        status=data.get("status"),
        cancel_at_period_end=bool(data.get("cancel_at_period_end")),
        entitlement_end=_stripe_ts(data.get("cancel_at"))
        or _stripe_ts(data.get("current_period_end")),
        billing_cycle_anchor=_stripe_ts(data.get("billing_cycle_anchor")),
    )
    await db.flush()
    _logger.info(
        "customer.subscription.updated: user %s -> %s (%s)",
        user.id, plan_name, outcome,
    )
    # A DEFERRED downgrade must not pause the customer's scrapers yet — they
    # keep the plan they paid for until the boundary, and the rollover triggers
    # reconciliation then (via reconcile_quota_periods).
    if outcome != "downgrade_pending":
        from src.api.entitlements import apply_reconciliation_async
        await apply_reconciliation_async(db, str(user.id), user.plan)


async def _handle_subscription_deleted(data: dict, db: AsyncSession) -> None:
    """P6b/P6c — the paid term has actually ended. Downgrade to Starter.

    Stripe sends this event both when a cancel-at-period-end reaches its end and
    when a subscription is cancelled immediately, so one handler covers both.

    ``records_used``, the entitlement window and the anchor are left ALONE: the
    customer already received those records, so refunding the counter would be a
    small free grant on every cancellation and would break the invariant that no
    plan change ever resets quota. They regain quota at their own next boundary,
    at the Starter cap. ``paid_entitlement_ended_at`` is stamped here, and it is
    the only thing that later lets a genuine resubscribe mint a fresh window.
    """
    customer_id = data.get("customer")
    if not customer_id:
        return

    # Locked BEFORE Stripe is asked for a survivor, for the same reason as in
    # _handle_subscription_updated: a user loaded unlocked here could be
    # overwritten with stale state after a concurrent update committed, and
    # a live payer downgraded. (Codex)
    result = await db.execute(
        select(User).where(User.stripe_customer_id == customer_id).with_for_update()
    )
    user = result.scalar_one_or_none()
    if user:
        # Which subscription ended matters. Cancelling one of two live
        # subscriptions (a stray duplicate, or the recorded one while the other
        # survives), or an old one we no longer track, must not downgrade an
        # account that is still paying, or still inside a promotion, on another.
        # So Stripe is asked every time which entitled plan subscription
        # remains; failing to ask raises and Stripe retries the event.
        deleted_id = data.get("id")
        try:
            # Every subscription, not the first non-terminal one: an `incomplete`
            # stray listed ahead of the active subscription must not hide it. A
            # survivor must carry a price we sell, or it grants nothing.
            survivor = next(
                (
                    s for s in stripe.Subscription.list(
                        customer=customer_id, status="all", limit=100,
                    ).auto_paging_iter()
                    if s.get("id") != deleted_id
                    and s.get("status") in _ENTITLED_CHECKOUT_STATUSES
                    and _plan_item_price_id(((s.get("items") or {}).get("data")) or [])
                ),
                None,
            )
        except Exception as exc:  # noqa: BLE001 - unknown must not read as "none left"
            _logger.error(
                "customer.subscription.deleted: %s ended for user %s and Stripe "
                "could not be asked what remains (%s); retrying later",
                deleted_id, user.id, str(exc)[:200],
            )
            raise
        if survivor is not None:
            _logger.warning(
                "customer.subscription.deleted: %s ended but user %s is still "
                "entitled on %s (%s); rebinding instead of downgrading",
                deleted_id, user.id, survivor.get("id"), survivor.get("status"),
            )
            if survivor.get("id") != user.stripe_subscription_id:
                # The ordinary update path re-reads it and applies its plan.
                await _handle_subscription_updated(
                    {"id": survivor.get("id"), "customer": customer_id}, db
                )
            return
        end_subscription(user)
        await db.flush()
        from src.api.entitlements import apply_reconciliation_async
        await apply_reconciliation_async(db, str(user.id), user.plan)


def _invoice_subscription_id(invoice: dict) -> str | None:
    """The subscription an invoice belongs to, on every webhook API version.

    Webhook payloads are rendered in the ENDPOINT's API version, not the SDK's.
    From 2025-03-31 onward Stripe removed the top-level `invoice.subscription`
    and moved it to `invoice.parent.subscription_details.subscription`. Reading
    only the old field makes every invoice look subscription-less on a newer
    endpoint, so dunning would never start and never clear.
    """
    legacy = invoice.get("subscription")
    if legacy:
        return legacy if isinstance(legacy, str) else legacy.get("id")
    details = (invoice.get("parent") or {}).get("subscription_details") or {}
    current = details.get("subscription")
    if isinstance(current, dict):
        return current.get("id")
    return current or None


async def _handle_payment_failed(data: dict, db: AsyncSession) -> list[Callable[[], None]]:
    """Start dunning if Stripe says the subscription is in it NOW, and notify.

    Judged on Stripe's CURRENT state, never on the event body. Stripe retries a
    webhook for three days and does not order deliveries, so a failure for an
    invoice that has since been paid can arrive after the recovery was already
    applied; trusted as-is it would re-freeze a paying customer and email them
    about a payment that went through. Both reads happen under the user's row
    lock, so a concurrent ``customer.subscription.updated`` cannot interleave.

    Deliberately narrow: this handler only ever starts the grace and records a
    dunning status. It never changes the plan, the limits or the window, and it
    never clears dunning; ``customer.subscription.updated`` owns recovery.
    """
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
        return []

    result = await db.execute(
        select(User).where(User.stripe_customer_id == customer_id).with_for_update()
    )
    user = result.scalar_one_or_none()
    if not user:
        return []

    # A failed read raises: no ledger row is written and Stripe retries, which
    # beats acting on a body that may describe the past. An invoice event with
    # no id cannot be checked against Stripe at all, so it raises too rather
    # than notifying or freezing on trust.
    invoice_id = data.get("id")
    if not invoice_id:
        raise ValueError("invoice.payment_failed event carries no invoice id")
    invoice_status = stripe.Invoice.retrieve(invoice_id).get("status")

    # P7: start the dunning grace. Until it expires the customer is served
    # normally (Stripe's retries span days, and freezing someone whose card
    # succeeds on retry 3 would be a self-inflicted outage). After it, the
    # account freezes: no new billable work, and the entitlement window stops
    # advancing, so an unpaid subscription cannot accrue a fresh bucket every
    # month. Nothing is deleted; results and past exports stay available.
    #
    # Only the RECORDED subscription, and only while Stripe itself has it in
    # dunning. Following the subscription's status rather than the individual
    # invoice is the product rule: Stripe's retry settings decide when a
    # subscription is past_due, so a failed skip-trace overage charge on an
    # otherwise healthy subscription does not freeze the plan on its own.
    invoice_sub = _invoice_subscription_id(data)
    if invoice_sub and invoice_sub == user.stripe_subscription_id:
        status = stripe.Subscription.retrieve(invoice_sub).get("status")
        if status in _DUNNING_SUBSCRIPTION_STATUSES:
            grace_until = mark_payment_failed(user)
            user.subscription_status = status
            await db.flush()
            _logger.info(
                "invoice.payment_failed: user %s (%s) served until %s, then frozen",
                user.id, status, grace_until.isoformat(),
            )
        else:
            _logger.info(
                "invoice.payment_failed: subscription %s for user %s is %s now; "
                "dunning not started", invoice_sub, user.id, status,
            )

    if invoice_status != "open":
        # Paid, voided or written off since the attempt failed: telling the
        # customer their payment failed would be wrong.
        _logger.info(
            "invoice.payment_failed: invoice %s is %s now; no notification sent",
            invoice_id, invoice_status,
        )
        return []

    # Returned, not sent: the webhook route sends them only after this change
    # commits (see stripe_webhook). Imported here to avoid a circular import at
    # startup. Values are captured now; the ORM row expires on commit.
    from src.workers.delivery import _send_payment_failed_email
    return [
        partial(_send_payment_failed_email, user.email, attempt_count),
        partial(_enqueue_payment_notification, str(user.id), attempt_count),
    ]


def _enqueue_payment_notification(user_id: str, attempt_count: int) -> None:
    """Phase 2b in-app notification, written by the worker/system path.

    The webhook session has no user RLS GUC, so notifications are never
    written from here directly.
    """
    from src.workers.tasks import emit_payment_notification
    emit_payment_notification.delay(user_id, attempt_count)
