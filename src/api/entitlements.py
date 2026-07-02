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
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import func, select, text
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

    # TOCTOU fix: when enforcing, serialize concurrent creates for THIS user with a
    # per-user advisory xact lock so the distinct-county count below can't be raced.
    # Transaction-scoped: held until the route commits its new config, so a second
    # concurrent create blocks until the first is visible. No-op in audit mode (the
    # count blocks nothing there, so the lock would only add needless serialization).
    # Namespaced classid 4242 ("entitlement") to avoid collision with other locks.
    if settings.ENTITLEMENT_ENFORCEMENT:
        await db.execute(
            text("SELECT pg_advisory_xact_lock(4242, hashtext(:uid))"),
            {"uid": str(user.id)},
        )

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


# ── Runtime (execution-time) entitlement helpers ─────────────────────────────
PAUSED_REASON_ENTITLEMENT = "entitlement"


@dataclass(frozen=True)
class ConfigRow:
    """Minimal projection of a ScraperConfig for entitlement math. Decoupled from
    the ORM so the logic is pure and unit-testable."""

    id: str
    state: str
    county: str
    record_type: str
    created_at: datetime
    active: bool = True
    paused_reason: str | None = None


def allowed_county_set(
    rows: Iterable[ConfigRow], plan: str
) -> set[tuple[str, str]] | None:
    """Normalized (STATE, county) jurisdictions the plan permits, chosen
    deterministically. Only configs whose record_type is ALLOWED for the plan can
    claim a slot (a disallowed-type config is paused on type grounds and must not
    evict a valid county). ACTIVE configs claim slots first (earliest created_at
    wins); entitlement-paused configs fill only remaining slots. None = unlimited."""
    plan = (plan or "starter").lower()
    cap = COUNTY_LIMIT_BY_PLAN.get(plan, COUNTY_LIMIT_BY_PLAN["starter"])
    if cap < 0:
        return None
    allowed_types = RECORD_TYPES_BY_PLAN.get(plan, RECORD_TYPES_BY_PLAN["starter"])
    active_earliest: dict[tuple[str, str], datetime] = {}
    paused_earliest: dict[tuple[str, str], datetime] = {}
    for row in rows:
        if row.record_type.lower() not in allowed_types:
            continue  # disallowed-type config: paused on type, never holds a slot
        key = _norm_county(row.state, row.county)
        if row.active:
            if key not in active_earliest or row.created_at < active_earliest[key]:
                active_earliest[key] = row.created_at
        elif row.paused_reason == PAUSED_REASON_ENTITLEMENT:
            if key not in paused_earliest or row.created_at < paused_earliest[key]:
                paused_earliest[key] = row.created_at
    chosen = [k for k, _ in sorted(active_earliest.items(), key=lambda kv: (kv[1], kv[0]))]
    chosen = chosen[:cap]
    remaining = cap - len(chosen)
    if remaining > 0:
        chosen_set = set(chosen)
        paused_ranked = sorted(
            ((k, t) for k, t in paused_earliest.items() if k not in chosen_set),
            key=lambda kv: (kv[1], kv[0]),
        )
        chosen.extend(k for k, _ in paused_ranked[:remaining])
    return set(chosen)


def config_run_violation(
    plan: str,
    state: str,
    county: str,
    record_type: str,
    active_rows: Iterable[ConfigRow],
) -> str | None:
    """Return a human-readable reason if running this (county, record_type) is NOT
    permitted under the user's CURRENT plan, else None. Fails closed on unknown plan."""
    plan = (plan or "starter").lower()
    rt = (record_type or "").lower()
    allowed_types = RECORD_TYPES_BY_PLAN.get(plan, RECORD_TYPES_BY_PLAN["starter"])
    if rt not in allowed_types:
        return (
            f"record type '{rt}' is not in your '{plan}' plan "
            f"(allowed: {sorted(allowed_types)})"
        )
    allowed = allowed_county_set(active_rows, plan)
    if allowed is not None:
        key = _norm_county(state, county)
        if key not in allowed:
            cap = COUNTY_LIMIT_BY_PLAN.get(plan, COUNTY_LIMIT_BY_PLAN["starter"])
            return (
                f"county {key[1]}, {key[0]} is outside your '{plan}' plan's "
                f"{cap}-county limit"
            )
    return None


def enforce_runnable_http(violation: str | None, *, user: User, context: str) -> None:
    """API call sites: raise 402 when enforcement is ON and a violation exists,
    else audit-log. No-op when violation is None."""
    if not violation:
        return
    if settings.ENTITLEMENT_ENFORCEMENT:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"Plan limit reached — {violation}. Upgrade your plan to continue.",
        )
    _logger.info(
        "entitlement audit (NOT enforced) user=%s plan=%s context=%s would_block: %s",
        user.id, _plan_of(user), context, violation,
    )


def should_block_run(violation: str | None, *, user_id: str, plan: str, context: str) -> bool:
    """Worker/scheduler call sites: returns True (caller must block/skip/fail) only
    when enforcement is ON and a violation exists; always audit-logs the would-block."""
    if not violation:
        return False
    _logger.info(
        "entitlement audit user=%s plan=%s context=%s would_block: %s",
        user_id, plan, context, violation,
    )
    return settings.ENTITLEMENT_ENFORCEMENT


def plan_reconciliation(
    rows: Iterable[ConfigRow], plan: str
) -> tuple[set[str], set[str]]:
    """Given ALL of a user's configs, return (pause_ids, revive_ids) for a plan.

    pause_ids  = currently-active configs no longer permitted under `plan`.
    revive_ids = entitlement-paused configs now permitted again.
    User-paused configs (paused_reason None, active False) are never touched."""
    rows = list(rows)
    plan = (plan or "starter").lower()
    allowed_counties = allowed_county_set(rows, plan)
    allowed_types = RECORD_TYPES_BY_PLAN.get(plan, RECORD_TYPES_BY_PLAN["starter"])

    def _permitted(r: ConfigRow) -> bool:
        if r.record_type.lower() not in allowed_types:
            return False
        if allowed_counties is None:
            return True
        return _norm_county(r.state, r.county) in allowed_counties

    pause_ids: set[str] = set()
    revive_ids: set[str] = set()
    for r in rows:
        if r.active and not _permitted(r):
            pause_ids.add(r.id)
        elif (not r.active) and r.paused_reason == PAUSED_REASON_ENTITLEMENT and _permitted(r):
            revive_ids.add(r.id)
    return pause_ids, revive_ids


# ── DB wrappers — thin persistence layer around plan_reconciliation ───────────

async def apply_reconciliation_async(
    db: AsyncSession,
    user_id: str,
    plan: str,
) -> tuple[int, int]:
    """Load all configs for *user_id*, run plan_reconciliation, persist changes.

    Returns (paused_count, revived_count). Caller is responsible for committing.
    Do NOT call inside a nested transaction that already holds row locks on
    scraper_configs — this issues its own SELECT + individual UPDATEs."""
    rows_result = await db.execute(
        select(ScraperConfig).where(ScraperConfig.user_id == user_id)
    )
    configs = rows_result.scalars().all()

    config_rows = [
        ConfigRow(
            id=str(c.id),
            state=c.state or "",
            county=c.county or "",
            record_type=c.record_type or "",
            created_at=c.created_at if c.created_at is not None else datetime.min.replace(tzinfo=None),
            active=bool(c.active),
            paused_reason=c.paused_reason,
        )
        for c in configs
    ]

    pause_ids, revive_ids = plan_reconciliation(config_rows, plan)

    if not settings.ENTITLEMENT_ENFORCEMENT:
        if pause_ids or revive_ids:
            _logger.info(
                "reconcile DRY-RUN (audit mode, not applied) user=%s plan=%s "
                "would_pause=%d would_revive=%d",
                user_id, plan, len(pause_ids), len(revive_ids),
            )
        return 0, 0

    config_by_id = {str(c.id): c for c in configs}
    for cid in pause_ids:
        cfg = config_by_id.get(cid)
        if cfg is not None:
            cfg.active = False
            cfg.paused_reason = PAUSED_REASON_ENTITLEMENT
    for cid in revive_ids:
        cfg = config_by_id.get(cid)
        if cfg is not None:
            cfg.active = True
            cfg.paused_reason = None

    if pause_ids or revive_ids:
        _logger.info(
            "reconciliation user=%s plan=%s paused=%d revived=%d",
            user_id, plan, len(pause_ids), len(revive_ids),
        )

    return len(pause_ids), len(revive_ids)


def apply_reconciliation_sync(
    db: object,
    user_id: str,
    plan: str,
) -> tuple[int, int]:
    """Synchronous variant for Celery beat tasks (SyncSessionLocal context).

    Returns (paused_count, revived_count). Caller is responsible for committing."""
    from sqlalchemy import select as _select

    rows_result = db.execute(_select(ScraperConfig).where(ScraperConfig.user_id == user_id))  # type: ignore[union-attr]
    configs = rows_result.scalars().all()

    config_rows = [
        ConfigRow(
            id=str(c.id),
            state=c.state or "",
            county=c.county or "",
            record_type=c.record_type or "",
            created_at=c.created_at if c.created_at is not None else datetime.min.replace(tzinfo=None),
            active=bool(c.active),
            paused_reason=c.paused_reason,
        )
        for c in configs
    ]

    pause_ids, revive_ids = plan_reconciliation(config_rows, plan)

    if not settings.ENTITLEMENT_ENFORCEMENT:
        if pause_ids or revive_ids:
            _logger.info(
                "reconcile DRY-RUN (audit mode, not applied) user=%s plan=%s "
                "would_pause=%d would_revive=%d",
                user_id, plan, len(pause_ids), len(revive_ids),
            )
        return 0, 0

    config_by_id = {str(c.id): c for c in configs}
    for cid in pause_ids:
        cfg = config_by_id.get(cid)
        if cfg is not None:
            cfg.active = False
            cfg.paused_reason = PAUSED_REASON_ENTITLEMENT
    for cid in revive_ids:
        cfg = config_by_id.get(cid)
        if cfg is not None:
            cfg.active = True
            cfg.paused_reason = None

    if pause_ids or revive_ids:
        _logger.info(
            "reconciliation user=%s plan=%s paused=%d revived=%d",
            user_id, plan, len(pause_ids), len(revive_ids),
        )

    return len(pause_ids), len(revive_ids)
