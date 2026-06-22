"""Per-tier entitlement validation — the value-metric layer.

Two checks, centralized so the scraper-create and batch-create routes (and any
future internal flow) enforce identically:
  1. record-type gating  — is each requested record_type allowed for the plan?
  2. distinct-county cap  — would this push the user past their plan's count of
                            distinct counties? (count-based: any N counties.)

ENFORCEMENT IS FEATURE-FLAGGED via ``settings.ENTITLEMENT_ENFORCEMENT``.

- FALSE (default): audit/log-only. Violations are LOGGED ("would block") and the
  request proceeds. This ships the infrastructure and lets us measure who would
  be affected WITHOUT (a) reversing the just-shipped "all paid plans access all
  counties" marketing, or (b) locking out any of the ~144 existing accounts.
- TRUE: the same violations raise HTTP 402.

Before flipping the flag in prod: update pricing/UI/error copy, intentionally
grandfather existing accounts, and harden the distinct-county count against the
concurrent-create race noted below.

Matrix lives in src/config/constants.py (single source of truth).
"""
from __future__ import annotations

from collections.abc import Iterable

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.constants import (
    COUNTY_LIMIT_BY_PLAN,
    RECORD_TYPES_BY_PLAN,
)
from src.config.settings import settings
from src.db.models import ScraperConfig, User
from src.utils.logger import setup_logger

_logger = setup_logger("api.entitlements")


def _plan_of(user: User) -> str:
    return (user.plan or "starter").lower()


def record_type_violations(plan: str, record_types: Iterable[str]) -> set[str]:
    """Return the requested record types NOT allowed for this plan (lowercased).

    Fails CLOSED: an unknown/typo'd plan is treated as the most restrictive tier
    (starter), never as "all types allowed".
    """
    allowed = RECORD_TYPES_BY_PLAN.get(plan, RECORD_TYPES_BY_PLAN["starter"])
    return {rt.lower() for rt in record_types} - allowed


def _norm_county(state: str, county: str) -> tuple[str, str]:
    """A county jurisdiction is identified by (STATE, county) — same county name
    can exist in different states. Trim + case-fold both so legacy rows that were
    only lowercased (e.g. 'king ' vs 'king') don't double-count."""
    return (state or "").strip().upper(), (county or "").strip().lower()


async def projected_county_overage(
    db: AsyncSession,
    user_id: str,
    plan: str,
    state: str,
    new_counties: Iterable[str],
) -> tuple[int, int] | None:
    """Return (projected_distinct_total, cap) if adding ``new_counties`` (all in
    ``state``) would exceed the plan's distinct-county cap, else None.

    Counts DISTINCT normalized (state, county) jurisdictions across the user's
    ACTIVE scraper configs (explicit user_id filter = multi-tenant suspenders on
    top of RLS), unioned with the counties being added. Fails CLOSED on an
    unknown plan (starter cap). -1 cap = unlimited (returns None).

    NOTE (pre-enforcement TODO): this read-then-decide is not atomic — two
    concurrent creates could each pass when enforcement is ON. Harmless while the
    flag is OFF; before flipping, gate the create in one transaction (e.g. lock
    the user row) so the count can't be raced.
    """
    cap = COUNTY_LIMIT_BY_PLAN.get(plan, COUNTY_LIMIT_BY_PLAN["starter"])
    if cap < 0:
        return None  # unlimited
    incoming = {_norm_county(state, c) for c in new_counties}
    rows = await db.execute(
        select(
            func.upper(func.trim(ScraperConfig.state)),
            func.lower(func.trim(ScraperConfig.county)),
        )
        .where(ScraperConfig.user_id == user_id, ScraperConfig.active)
        .distinct()
    )
    existing = {(r[0], r[1]) for r in rows.all()}
    projected = len(existing | incoming)
    if projected > cap:
        return projected, cap
    return None


async def enforce_entitlements(
    db: AsyncSession,
    user: User,
    *,
    state: str,
    counties: Iterable[str],
    record_types: Iterable[str],
    context: str,
) -> None:
    """Validate county + record-type entitlements for a create request.

    ``state`` is the (single) state the requested counties belong to — both the
    scraper-create and batch-create flows scope one state per request.

    Raises HTTP 402 when ``settings.ENTITLEMENT_ENFORCEMENT`` is true and a limit
    is exceeded; otherwise logs the would-block at INFO and returns (audit mode).
    """
    plan = _plan_of(user)
    problems: list[str] = []

    bad_types = record_type_violations(plan, record_types)
    if bad_types:
        allowed = sorted(RECORD_TYPES_BY_PLAN.get(plan, RECORD_TYPES_BY_PLAN["starter"]))
        problems.append(
            f"record type(s) {sorted(bad_types)} are not in your '{plan}' plan "
            f"(allowed: {allowed})"
        )

    overage = await projected_county_overage(db, user.id, plan, state, counties)
    if overage is not None:
        projected, cap = overage
        problems.append(
            f"this would span {projected} distinct counties but your '{plan}' "
            f"plan allows {cap}"
        )

    if not problems:
        return

    summary = "; ".join(problems)
    if settings.ENTITLEMENT_ENFORCEMENT:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"Plan limit reached — {summary}. Upgrade your plan to continue.",
        )
    # Audit/log-only: infrastructure shipped, enforcement deferred.
    _logger.info(
        "entitlement audit (NOT enforced) user=%s plan=%s context=%s would_block: %s",
        user.id, plan, context, summary,
    )
