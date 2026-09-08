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
    count_label,
    record_type_label,
)
from src.config.plans import plan_label
from src.config.settings import settings
from src.db.models import ScraperConfig, User
from src.utils.logger import setup_logger

_logger = setup_logger("api.entitlements")


def _plan_of(user: User) -> str:
    return (user.plan or "starter").lower()


# --- Customer-facing copy ---------------------------------------------------
# A plan limit is NOT an application error: the product is working, the account
# simply does not cover what was asked for. Every string below is written to be
# read by a customer, so: no slugs, no quoted plan names, no internal vocabulary
# ("distinct counties"), no em dashes, and pluralization that is actually right.
CODE_COUNTY_LIMIT = "county_limit"
CODE_RECORD_TYPE = "record_type"
CODE_PLAN_LIMIT = "plan_limit"


@dataclass(frozen=True)
class Violation:
    """One entitlement failure, carried as structured data rather than prose.

    ``__str__`` is the message, so the pre-existing consumers that interpolate a
    violation into a log line or a job-failure reason keep working unchanged.
    Do NOT classify a violation by regex-matching its message: read ``code``.
    """

    code: str
    title: str
    message: str

    def __str__(self) -> str:
        return self.message


def _join_english(items: list[str]) -> str:
    """['A'] -> 'A'; ['A','B'] -> 'A and B'; ['A','B','C'] -> 'A, B and C'."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _county_word(n: int) -> str:
    return count_label(n, "county", "counties")


def county_cap_violation(plan: str, projected: int, cap: int) -> Violation:
    """Create-time: this request would push the account past its county cap.

    ``projected`` is the account-wide distinct-county total, NOT the count in
    this one request, so the copy says "your account". That keeps it true whether
    the caller is a single scraper create or a batch fan-out, and true when the
    overage comes from scrapers the user already saved.
    """
    return Violation(
        code=CODE_COUNTY_LIMIT,
        title="County limit reached",
        message=(
            f"Your {plan_label(plan)} plan includes {_county_word(cap)}. "
            f"This would put your account at {_county_word(projected)}."
        ),
    )


def county_outside_plan_violation(plan: str, state: str, county: str, cap: int) -> Violation:
    """Run-time: this county is not one of the ones the plan currently covers."""
    where = f"{(county or '').strip().title()}, {(state or '').strip().upper()}"
    return Violation(
        code=CODE_COUNTY_LIMIT,
        title="County limit reached",
        message=(
            f"Your {plan_label(plan)} plan includes {_county_word(cap)}. "
            f"{where} is outside that limit."
        ),
    )


def record_type_violation(plan: str, record_types: Iterable[str]) -> Violation:
    """Create- and run-time: one or more requested record types are not covered.

    Deliberately does NOT enumerate what the plan DOES include: on Business that
    is a seven-item list that buries the one thing the reader needs to know.
    """
    labels = sorted({record_type_label(rt) for rt in record_types})
    subject = _join_english(labels)
    verb = "is" if len(labels) == 1 else "are"
    return Violation(
        code=CODE_RECORD_TYPE,
        title="Record type not in your plan",
        message=f"{subject} {verb} not included in your {plan_label(plan)} plan.",
    )


def combine_violations(violations: list[Violation]) -> Violation:
    """One notice for the whole request. A create can break the county cap AND
    the record-type matrix at once; showing only the first would send the user
    back for a second rejection after they fixed it."""
    if len(violations) == 1:
        return violations[0]
    return Violation(
        code=CODE_PLAN_LIMIT,
        title="Plan limit reached",
        message=" ".join(v.message for v in violations),
    )


# Appended to the message on the wire so a non-browser API consumer (which has no
# Upgrade button to read) still gets a complete instruction.
_UPGRADE_SENTENCE = "Upgrade your plan to continue."


def _plan_limit_http(violation: Violation) -> HTTPException:
    """402 whose body is structured, not prose.

    The frontend renders ``title`` and ``message`` as a calm plan notice, so the
    copy must not be reverse-engineered from a sentence. FastAPI serializes a
    dict detail as-is, and main.py registers no HTTPException handler that would
    stringify it. Older clients that read ``detail`` as a string will now see an
    object, so the tolerant frontend parser has to ship first.
    """
    return HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "code": violation.code,
            "title": violation.title,
            "message": f"{violation.message} {_UPGRADE_SENTENCE}",
        },
    )


def disallowed_record_types(plan: str, record_types: Iterable[str]) -> set[str]:
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

    CONCURRENCY: this read-then-decide is not atomic on its own, but the caller
    closes the race. enforce_entitlements() takes a per-user
    pg_advisory_xact_lock BEFORE calling this whenever ENTITLEMENT_ENFORCEMENT is
    on, and that lock is transaction-scoped, so it is still held when the route
    inserts the new config and is only released at commit. Both create paths are
    covered: routes/scrapers.py create_scraper and routes/batches.py both declare
    Depends(get_rls_db), and get_rls_db wraps get_db (src/api/deps.py) -- it only
    issues set_config(..., local=true) on that same session and returns it, so the
    single commit is still get_db's teardown, AFTER the insert. An earlier version
    of this line said scrapers.py used get_db directly, implying the two routes
    differed; they do not. Verify the route signature, not this sentence.
    Neither path commits between the check and the insert. Do NOT read this function
    in isolation and conclude the count can be raced -- an earlier version of
    this note said the gating was still outstanding long after it had landed,
    which is worse than no note at all.
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

    problems: list[Violation] = []

    bad_types = disallowed_record_types(plan, record_types)
    if bad_types:
        problems.append(record_type_violation(plan, bad_types))

    overage = await projected_county_overage(db, user.id, plan, state, counties)
    if overage is not None:
        projected, cap = overage
        problems.append(county_cap_violation(plan, projected, cap))

    if not problems:
        return

    summary = " ".join(v.message for v in problems)
    if settings.ENTITLEMENT_ENFORCEMENT:
        raise _plan_limit_http(combine_violations(problems))
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
) -> Violation | None:
    """Return why running this (county, record_type) is NOT permitted under the
    user's CURRENT plan, else None. Fails closed on unknown plan.

    Returns a ``Violation``, not a bare string, so the HTTP wrapper can render a
    titled notice without parsing English. ``Violation.__str__`` is the message,
    so the worker/scheduler call sites that interpolate the result into a log line
    or a job-failure reason keep working unchanged."""
    plan = (plan or "starter").lower()
    rt = (record_type or "").lower()
    allowed_types = RECORD_TYPES_BY_PLAN.get(plan, RECORD_TYPES_BY_PLAN["starter"])
    if rt not in allowed_types:
        return record_type_violation(plan, [rt])
    allowed = allowed_county_set(active_rows, plan)
    if allowed is not None:
        key = _norm_county(state, county)
        if key not in allowed:
            cap = COUNTY_LIMIT_BY_PLAN.get(plan, COUNTY_LIMIT_BY_PLAN["starter"])
            return county_outside_plan_violation(plan, state, county, cap)
    return None


def enforce_runnable_http(violation: Violation | None, *, user: User, context: str) -> None:
    """API call sites: raise 402 when enforcement is ON and a violation exists,
    else audit-log. No-op when violation is None."""
    if not violation:
        return
    if settings.ENTITLEMENT_ENFORCEMENT:
        raise _plan_limit_http(violation)
    _logger.info(
        "entitlement audit (NOT enforced) user=%s plan=%s context=%s would_block: %s",
        user.id, _plan_of(user), context, violation,
    )


def should_block_run(
    violation: Violation | None, *, user_id: str, plan: str, context: str
) -> bool:
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
