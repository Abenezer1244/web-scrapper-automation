"""Per-scraper run eligibility: may THIS scraper start a run, and if not, why.

The one evaluator behind both ``POST /jobs`` (which raises from its answer) and
``GET /scrapers`` (which reports it), so the Run now button cannot disagree with
the gate (UX audit Q6 / F-035). It returns data and never raises HTTP errors;
only the route maps a code to a response.

Codes, in the order the gate refuses (the reason shown is the refusal the user
would actually get):

  config_inactive  deleted or entitlement-paused. PAGE-ONLY: POST /jobs answers
                   an inactive config with a plain 404 before any of this.
  run_in_flight    the scraper already holds its run slot (Job.holds_run_slot).
  not_entitled     the plan does not include this record type or county. Blocks
                   only while ENTITLEMENT_ENFORCEMENT is on (it is in prod).
  ai_limit         the monthly AI scrape limit is used up (CALENDAR month, UTC).
  frozen | ended | over_limit
                   the account rule, ``src.api.quota.run_eligibility``.

Batched: a list costs a fixed number of queries, however many scrapers it has.
Every job query is scoped by ``Job.user_id`` AND the caller's own configs.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.entitlements import (
    PAUSED_REASON_ENTITLEMENT,
    ConfigRow,
    Violation,
    config_run_violation,
    plan_limit_http,
)
from src.api.quota import run_eligibility
from src.config import settings
from src.config.constants import normalize_plan
from src.db.models import CountyConnector, Job, ScraperConfig
from src.scrapers.registry import pick_connector

RUN_IN_FLIGHT_MESSAGE = "This scraper is already running."
RUN_STOPPING_MESSAGE = "This scraper is still stopping. Try again in a few minutes."
_DELETED_MESSAGE = "This scraper has been deleted."
_PAUSED_MESSAGE = (
    "This scraper is paused because your current plan does not include it. "
    "Upgrade your plan to resume it."
)


def run_in_flight_message(stopping: bool) -> str:
    """The 409 run_in_flight message, shared by the gate and the page."""
    return RUN_STOPPING_MESSAGE if stopping else RUN_IN_FLIGHT_MESSAGE


def ai_limit_message(used: int, limit: int) -> str:
    return (
        f"Monthly AI scrape limit reached ({used}/{limit}). "
        "Upgrade your plan for more AI-powered scrapes."
    )


def month_start(now: datetime) -> datetime:
    """First instant of ``now``'s UTC calendar month: the AI counter's period."""
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_month_start(now: datetime) -> datetime:
    start = month_start(now)
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


@dataclass(frozen=True)
class ConfigRunEligibility:
    """The answer for one scraper. ``violation`` is the entitlement failure the
    gate raises (or audit-logs, with enforcement off), whatever ``code`` won."""

    can_run: bool
    code: str | None = None
    message: str | None = None
    resumes_at: datetime | None = None
    job_id: str | None = None
    violation_code: str | None = None
    violation: Violation | None = field(default=None, compare=False, repr=False)


def _jurisdiction(state: str, county: str) -> tuple[str, str]:
    return (state or "").lower(), (county or "").lower()


async def _active_connectors(
    db: AsyncSession, jurisdictions: Iterable[tuple[str, str]]
) -> dict[tuple[str, str], list]:
    """Every ACTIVE connector (manual and ai) for these (state, county) pairs."""
    wanted = sorted(set(jurisdictions))
    if not wanted:
        return {}
    rows = (await db.execute(
        select(CountyConnector).where(
            CountyConnector.active,
            tuple_(func.lower(CountyConnector.state), func.lower(CountyConnector.county)).in_(
                wanted
            ),
        )
    )).scalars().all()
    by_place: dict[tuple[str, str], list] = {}
    for connector in rows:
        by_place.setdefault(_jurisdiction(connector.state, connector.county), []).append(connector)
    return by_place


def _is_ai(connectors_by_place, state: str, county: str, record_type: str) -> bool:
    """True when the connector the WORKER would run for this is ai-mode."""
    chosen = pick_connector(connectors_by_place.get(_jurisdiction(state, county), []), record_type)
    return chosen is not None and chosen.scraper_mode == "ai"


async def config_run_eligibility(
    db: AsyncSession,
    user,
    configs: Sequence,
    now: datetime,
) -> dict[str, ConfigRunEligibility]:
    """Eligibility for each of ``configs`` (the caller's own), keyed by config id.

    ``now`` is the one clock for the whole answer: run slots, the AI month and
    the account window are all judged at it.
    """
    configs = list(configs)
    if not configs:
        return {}
    config_ids = [c.id for c in configs]

    # 1. Run slots: the newest slot-holding job per config, as _run_in_flight picks it.
    in_flight: dict[str, tuple[str, bool]] = {}
    slot_rows = (await db.execute(
        select(Job.scraper_config_id, Job.id, Job.status)
        .where(
            Job.user_id == user.id,
            Job.scraper_config_id.in_(config_ids),
            Job.holds_run_slot(now),
        )
        .order_by(Job.created_at.desc())
    )).all()
    for row in slot_rows:
        in_flight.setdefault(str(row.scraper_config_id), (str(row.id), row.status == "cancelled"))

    # 2. Entitlement slot math over ALL of the tenant's active configs, never just
    #    the ones being asked about.
    active_rows = [
        ConfigRow(*r)
        for r in (await db.execute(
            select(
                ScraperConfig.id, ScraperConfig.state, ScraperConfig.county,
                ScraperConfig.record_type, ScraperConfig.created_at,
                ScraperConfig.active, ScraperConfig.paused_reason,
            ).where(ScraperConfig.user_id == user.id, ScraperConfig.active)
        )).all()
    ]

    # 3. AI usage, classified by the connector the worker would actually run.
    plan = normalize_plan(user.plan)
    ai_limit = settings.AI_JOB_LIMITS.get(plan, settings.AI_JOB_LIMITS["starter"])
    connectors = await _active_connectors(
        db, (_jurisdiction(c.state, c.county) for c in configs)
    )
    ai_configs = {c.id for c in configs if _is_ai(connectors, c.state, c.county, c.record_type)}
    ai_used = 0
    if ai_configs and ai_limit != -1:
        month_jobs = (await db.execute(
            select(ScraperConfig.state, ScraperConfig.county, ScraperConfig.record_type)
            .select_from(Job)
            .join(
                ScraperConfig,
                (Job.scraper_config_id == ScraperConfig.id)
                & (ScraperConfig.user_id == user.id),
            )
            .where(Job.user_id == user.id, Job.created_at >= month_start(now))
        )).all()
        missing = {
            _jurisdiction(j.state, j.county) for j in month_jobs
        } - set(connectors)
        if missing:
            connectors.update(await _active_connectors(db, missing))
        ai_used = sum(
            1 for j in month_jobs if _is_ai(connectors, j.state, j.county, j.record_type)
        )

    # 4. The account rule, once.
    account = run_eligibility(user, now)

    result: dict[str, ConfigRunEligibility] = {}
    for config in configs:
        if not config.active:
            paused = config.paused_reason == PAUSED_REASON_ENTITLEMENT
            result[config.id] = ConfigRunEligibility(
                False, "config_inactive", _PAUSED_MESSAGE if paused else _DELETED_MESSAGE
            )
            continue

        # The config counts toward its OWN county claim, exactly as the gate has
        # always judged it (a replication gap must not evict it).
        rows = [*active_rows, ConfigRow(
            config.id, config.state, config.county, config.record_type,
            config.created_at or now, True, None,
        )]
        violation = config_run_violation(
            user.plan, config.state, config.county, config.record_type, rows
        )

        if config.id in in_flight:
            job_id, stopping = in_flight[config.id]
            result[config.id] = ConfigRunEligibility(
                False, "run_in_flight", run_in_flight_message(stopping),
                job_id=job_id, violation=violation,
            )
        elif violation is not None and settings.ENTITLEMENT_ENFORCEMENT:
            result[config.id] = ConfigRunEligibility(
                False, "not_entitled", plan_limit_http(violation).detail["message"],
                violation_code=violation.code, violation=violation,
            )
        elif config.id in ai_configs and ai_limit != -1 and ai_used >= ai_limit:
            result[config.id] = ConfigRunEligibility(
                False, "ai_limit", ai_limit_message(ai_used, ai_limit),
                resumes_at=next_month_start(now), violation=violation,
            )
        elif not account.can_run:
            result[config.id] = ConfigRunEligibility(
                False, account.code, account.message,
                resumes_at=account.resumes_at, violation=violation,
            )
        else:
            result[config.id] = ConfigRunEligibility(True, violation=violation)
    return result
