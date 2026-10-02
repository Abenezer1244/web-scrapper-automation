"""Job routes: CRUD + SSE live log stream."""

import asyncio
import concurrent.futures
import functools
import json
import secrets
import threading
import time
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import redis as _sync_redis
import redis.exceptions as _redis_exceptions
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import (
    ColumnElement,
    String,
    and_,
    false,
    func,
    not_,
    or_,
    select,
    text,
    type_coerce,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api import sse_leases
from src.api.auth import CurrentUser, get_auth_context
from src.api.deps import get_db, get_rls_db
from src.api.dialer_filters import dialer_ready_conditions
from src.api.entitlements import raise_plan_features, skip_trace_violation
from src.api.errors import run_refusal_http
from src.api.lead_actionability import actionable_condition, has_address_condition
from src.api.middleware import audit_log, rate_limit, sanitize_search
from src.api.owner_filters import build_owner_conditions
from src.api.quota import run_eligibility
from src.api.results_category import (
    DEFAULT_RESULTS_CATEGORY,
    ResultsCategory,
    already_delivered_condition,
    category_condition,
)
from src.api.results_sort import DEFAULT_RESULTS_SORT, ResultsSort, results_order_by
from src.api.routes.auth_helpers.registration import _integrity_error_fields
from src.api.run_breakdown import breakdown_from_job, live_breakdown, read_partition_async
from src.api.schemas import (
    RUN_START_402_RESPONSES,
    AlreadyDeliveredContacts,
    AuctionCoverage,
    ContactLookupAction,
    ContactLookupConfirmErrorResponse,
    ContactLookupConfirmRequest,
    ContactLookupExcluded,
    ContactLookupPause,
    ContactLookupQuote,
    ContactLookupQuoteRequest,
    ContactLookupUnavailableResponse,
    DuplicateSource,
    JobCreate,
    JobResponse,
    LogLine,
    ResultRow,
    ResultsPage,
    RunBreakdown,
)
from src.api.tax_filters import build_tax_conditions, tax_cap_condition
from src.config import settings
from src.config.constants import (
    AUCTION_PUBLICATION_LAG_DAYS,
    CANCELLABLE_STATUSES,
    SKIP_TRACE_ADDON_PLANS,
    normalize_plan,
    scrape_queue_for_plan,
)
from src.config.lookup_pricing import CURRENCY as LOOKUP_CURRENCY
from src.config.lookup_pricing import PRICING_VERSION as LOOKUP_PRICING_VERSION
from src.config.lookup_pricing import included_lookups_remaining, unit_price_cents
from src.db import Job, JobLog, Result, ScraperConfig, User
from src.db import session as db_session
from src.utils.logger import setup_logger
from src.utils.skip_trace_pause_state import UNKNOWN as PAUSE_UNKNOWN
from src.utils.skip_trace_pause_state import PauseState, read_pause_state

_logger = setup_logger("api.jobs")

# Live log stream (GET /jobs/{id}/logs).
_SSE_TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})
_SSE_MAX_DURATION_SECONDS = 1800
_SSE_STATUS_CHECK_SECONDS = 60
# How often the stream sends a comment line when the job is quiet.
#
# A scrape can produce nothing for minutes at a time — 401 consecutive seconds on
# the run that prompted this work — and until now the connection sent no bytes
# either. A dropped TCP connection is indistinguishable from a quiet one until
# something is written, so the client's "LIVE" indicator could sit green on a
# stream that had been dead for minutes, which is worse than showing nothing.
#
# A `:` comment is the SSE-native way to say "still here". It carries no event,
# clients skip it by spec, and this one is deliberately about the TRANSPORT only:
# it proves the stream is open, never that the worker is making progress. That
# second question is answered by progress_stalled on the job itself, and the two
# must not be conflated — a healthy connection to a dead worker is exactly the
# state the whole Live Run rework exists to stop misreporting.
_SSE_KEEPALIVE_SECONDS = 15
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",  # Disable nginx buffering for SSE
}

router = APIRouter(prefix="/jobs", tags=["jobs"])

# A stored phone/email "has content" when it holds a character outside this class.
# The leads table (FE `nonBlank`) tests the SAME class, so a row whose cell shows a
# contact is never summarised as "not looked up". Not PostgreSQL trim(): it strips
# only spaces, while JS String.trim() also strips tabs and newlines. Blanks are
# normalised to NULL at bind; this catches legacy plaintext stored before that
# (dialer_filters.py). A presence test only, never a match on the value. The column
# is coerced to plain String so the pattern binds as text, not through the
# encrypting type.
_CONTENT_PATTERN = r"[^ \t\n\r\f\v]"


def _has_content(column) -> ColumnElement[bool]:
    return func.coalesce(type_coerce(column, String), "").regexp_match(_CONTENT_PATTERN)


_HAS_CONTACT = or_(_has_content(Result.phone), _has_content(Result.email))
_STATUS = Result.skip_trace_status

# AlreadyDeliveredContacts buckets: disjoint predicates, counted in one statement.
# A lead still marked not_attempted that carries a contact is a legacy answered row
# (traced before the status existed): it counts as found, as the leads table shows it.
_CONTACT_BUCKETS = (
    ("found", or_(_STATUS == "hit", and_(_STATUS == "not_attempted", _HAS_CONTACT))),
    ("none_found", _STATUS == "miss"),
    ("looking", _STATUS.in_(("queued", "submitted"))),
    ("failed", _STATUS == "errored"),
    ("not_looked_up", and_(_STATUS == "not_attempted", not_(_HAS_CONTACT))),
    ("removed", _STATUS == "purged"),
)


def _run_delivered(job: Job) -> bool:
    """Whether this run's rows may leave the building (audit 2026-09-25, N-03).

    `done` is the only billed state: the quota reservation and the charge commit
    with it, and a cancelled or failed run is never charged. Segments and the
    batch combined export already apply the same rule (2026-09-08). `done` is
    terminal (`_set_status` never moves a job out of it), so this cannot flip back.
    """
    return job.status == "done"


def _undelivered_run_409() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "run_not_finished",
            "message": "This run has not finished, so there is nothing to download yet.",
        },
    )


def _snapshot_breakdown(job: Job) -> tuple[RunBreakdown | None, bool]:
    """The run-count breakdown the worker froze on this row, and whether a stored
    one was REJECTED.

    A stored snapshot that does not add up cannot happen by construction; if one
    ever does, nothing is shown rather than numbers that contradict each other, and
    the reason is logged (ids only, no row data). Rejected is not "absent": the
    caller must not fall back to the live partition for it, which could show
    post-backfill numbers in place of the run's own (fail closed, Codex 2c r4).
    """
    values, problem = breakdown_from_job(job)
    if problem is not None:
        _logger.warning("Job %s: stored run-count breakdown rejected (%s)", job.id, problem)
    return (RunBreakdown(**values) if values is not None else None), problem is not None


def _job_response(job: Job, config: ScraperConfig | None) -> JobResponse:
    """A JobResponse with the fields that live on the job's scraper config.

    The one place both the list and the single-job endpoints fill them. GET
    /jobs/{id} used to skip this, so record_type came back null there and the
    Results page could not tell an auction-lead job (its "Notice Date" header).
    """
    resp = JobResponse.model_validate(job)
    resp.breakdown, _rejected = _snapshot_breakdown(job)
    resp.breakdown_basis = "snapshot" if resp.breakdown is not None else None
    if config is not None:
        resp.scraper_name = config.name
        resp.county = config.county
        resp.state = config.state
        resp.record_type = config.record_type
        resp.batch_id = config.batch_id  # None for standalone; set for batch children
    return resp


@router.get("", response_model=list[JobResponse])
async def list_jobs(
    current_user: CurrentUser,
    exclude_batch_children: bool = Query(
        False,
        description=(
            "Exclude jobs that belong to a batch (their config carries a batch_id). "
            "The Results page sets this so a batch's child scrapes don't consume the "
            "newest-100 window. The batch shows as one combined row instead."
        ),
    ),
    db: AsyncSession = Depends(get_rls_db),
) -> list[JobResponse]:
    stmt = select(Job).where(Job.user_id == current_user.id)
    if exclude_batch_children:
        # Filter batch children out BEFORE the LIMIT (Codex P1) — annotating
        # batch_id post-fetch would let a large batch's children fill the window
        # and push standalone exports out of the response.
        stmt = stmt.join(
            ScraperConfig, ScraperConfig.id == Job.scraper_config_id
        ).where(
            # Belt-and-suspenders tenant scope on the join (Codex): RLS + the
            # Job.user_id filter already bound it, but every joined table carries
            # its own user_id predicate in this codebase.
            ScraperConfig.user_id == current_user.id,
            ScraperConfig.batch_id.is_(None),
        )
    result = await db.execute(stmt.order_by(Job.created_at.desc()).limit(100))
    jobs = result.scalars().all()

    # Batch-load scraper configs for all jobs in one query
    config_ids = list({j.scraper_config_id for j in jobs})
    config_map: dict[str, ScraperConfig] = {}
    if config_ids:
        configs_result = await db.execute(
            select(ScraperConfig).where(
                ScraperConfig.id.in_(config_ids),
                ScraperConfig.user_id == current_user.id,  # defense-in-depth owner filter
            )
        )
        config_map = {str(c.id): c for c in configs_result.scalars().all()}

    responses = []
    for j in jobs:
        responses.append(
            _job_response(j, config_map.get(str(j.scraper_config_id)))
        )
    return responses


# ─── One active run per scraper (UX audit F-003, migration 104) ──────────────
# The partial unique index is the authority; the pre-check below only turns the
# common case into a clear 409 before any other work.
ONE_ACTIVE_JOB_INDEX = "uq_jobs_one_active_per_config"


def _run_in_flight_http(job_id: str | None, *, stopping: bool) -> HTTPException:
    from src.api.config_eligibility import run_in_flight_message

    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "run_in_flight",
            "job_id": job_id,
            "message": run_in_flight_message(stopping),
        },
    )


async def _run_in_flight(db: AsyncSession, user_id, config_id) -> tuple[str, bool] | None:
    """The job holding this config's run slot, as (job_id, still_stopping), or None.
    Owner-scoped, so the id returned is always the caller's own job."""
    row = (await db.execute(
        select(Job.id, Job.status)
        .where(
            Job.scraper_config_id == config_id,
            Job.user_id == user_id,
            Job.holds_run_slot(),
        )
        .order_by(Job.created_at.desc())
        .limit(1)
    )).first()
    if row is None:
        return None
    return str(row.id), row.status == "cancelled"


async def enqueue_scrape_job(
    db: AsyncSession,
    current_user,
    config: "ScraperConfig",
    trigger: str,
    request: Request,
) -> "Job":
    """Enforce the run gates, create a pending Job for `config`, commit, then
    enqueue the Celery scrape task. POST /jobs is its only caller; the scheduler
    and the batch fan-out start runs through their own dispatch paths.

    The caller owns config lookup/creation; this helper never re-checks
    `config.active`.

    Every refusal comes from ``config_run_eligibility``, the same evaluator
    GET /scrapers reports, so Run now cannot disagree with this gate. In order:
      * 409 run_in_flight: a config with an active job, or one cancelled mid-run
        whose worker has not stopped yet (see Job.holds_run_slot);
      * 402 entitlement (structured), when the plan does not include this
        record type or county — audit-logged only while ENTITLEMENT_ENFORCEMENT
        is off. An existing config can outlive a downgrade, so this re-validates
        against the CURRENT plan;
      * 402 account rule (frozen / ended / over the record limit), which keeps
        the two reasons apart on purpose: "over your limit" sends a customer
        whose card failed to the upgrade page, which does not fix a payment.
    """
    from datetime import UTC, datetime

    from src.api.config_eligibility import config_run_eligibility
    from src.api.entitlements import enforce_runnable_http

    # One clock for every gate below.
    now = datetime.now(UTC)
    eligibility = (await config_run_eligibility(db, current_user, [config], now))[config.id]

    if eligibility.code == "run_in_flight":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "run_in_flight",
                "job_id": eligibility.job_id,
                "message": eligibility.message,
            },
        )
    # Raises the structured 402 when enforcing; audit-logs otherwise.
    enforce_runnable_http(eligibility.violation, user=current_user, context="create_job")
    if not eligibility.can_run:
        # The account rule: the same sentence in `detail` as always,
        # with the code and resumes_at added beside it (src/api/errors.py).
        raise run_refusal_http(eligibility.code, eligibility.message, eligibility.resumes_at)

    job = Job(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        scraper_config_id=config.id,
        status="pending",
        trigger=trigger,
    )
    # Plain values, read BEFORE the flush: a rollback below expires every ORM
    # object in the session, and reading an expired attribute lazy-loads, which an
    # async session cannot do (MissingGreenlet, a 500 instead of the 409).
    user_id, config_id = current_user.id, config.id
    db.add(job)
    try:
        await db.flush()
    except IntegrityError as exc:
        # Only the one-active-run index means "already running"; any other
        # violation is a real error and must not be dressed up as a 409.
        fields = _integrity_error_fields(exc)
        if fields.get("sqlstate") != "23505" or ONE_ACTIVE_JOB_INDEX not in (
            fields.get("constraint_name") or ""
        ):
            raise
        # A concurrent request won the race between our pre-check and insert.
        # Roll back first (the session is unusable until then; the RLS GUC is
        # re-applied on the next transaction), then name the winner. It may
        # already have finished, so the id can be None.
        await db.rollback()
        winner = await _run_in_flight(db, user_id, config_id)
        raise _run_in_flight_http(
            winner[0] if winner else None, stopping=bool(winner and winner[1])
        ) from None
    # Commit BEFORE enqueuing so the row is durably 'pending' when the worker
    # consumes the message. The worker claims the job with an atomic CAS
    # (UPDATE ... WHERE status='pending'); if we enqueued first and a worker
    # consumed the task before this transaction committed, that claim would see
    # no row (rowcount=0), bail to avoid a double-scrape, and the job would
    # commit orphaned in 'pending' forever (watchdog deliberately skips fresh
    # retry_count=0 pending). This is the same commit-then-enqueue contract the
    # batch fan-out already uses. get_db's teardown commit then no-ops on the
    # clean session; the after_begin listener re-applies the RLS GUC on the next
    # transaction (the is_local GUC self-clears on this commit).
    await db.commit()

    # Enqueue Celery task — paid plans get priority queue. If the broker publish
    # fails here the job is already committed 'pending'; the watchdog re-delivers
    # it (its atomic claim dedupes), so we never strand it.
    from src.workers.tasks import run_scrape_job

    queue = scrape_queue_for_plan(current_user.plan)
    try:
        run_scrape_job.apply_async(args=[job.id], queue=queue)
    except Exception:
        # Broker publish failed (e.g. Redis blip). The job is already durably
        # committed 'pending', so we do NOT 500: a 500 here would tell the client
        # the create failed and invite a retry, minting a DUPLICATE job for the
        # same intent. Instead we log and return the committed job — the watchdog
        # re-delivers orphaned fresh-pending rows within ~10 min and its atomic
        # claim dedupes, so the scrape still runs exactly once.
        _logger.warning(
            "run_scrape_job publish failed for job_id=%s; committed 'pending', "
            "leaving for watchdog re-delivery",
            job.id,
            exc_info=True,
        )

    audit_log(request, "job_created", current_user.id, f"job_id={job.id}")
    return job


@router.post(
    "",
    response_model=JobResponse,
    status_code=status.HTTP_201_CREATED,
    responses=RUN_START_402_RESPONSES,
)
async def create_job(
    body: JobCreate,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> JobResponse:
    await rate_limit(request, zone="jobs", identifier=current_user.id)

    # Verify scraper config belongs to user AND is active (a manual "Run now"
    # only targets a real, non-soft-deleted scraper).
    config_result = await db.execute(
        select(ScraperConfig).where(
            ScraperConfig.id == body.scraper_config_id,
            ScraperConfig.user_id == current_user.id,
            ScraperConfig.active,
        )
    )
    config = config_result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scraper not found")

    job = await enqueue_scrape_job(db, current_user, config, body.trigger, request)
    return _job_response(job, config)


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> JobResponse:
    result = await db.execute(
        select(Job).where(Job.id == job_id, Job.user_id == current_user.id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    config = (await db.execute(
        select(ScraperConfig).where(
            ScraperConfig.id == job.scraper_config_id,
            ScraperConfig.user_id == current_user.id,  # defense-in-depth owner filter
        )
    )).scalar_one_or_none()
    return _job_response(job, config)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_job(
    job_id: str,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> None:
    await rate_limit(request, zone="writes", identifier=current_user.id)  # audit #3 S3-09
    # One statement, so the status is checked against the row as it is when the
    # write lands. Checking in Python and then writing by primary key let a cancel
    # that read 'enriching' overwrite the worker's just-committed billed 'done':
    # the customer was charged and holds a download, but the job read cancelled
    # and its paid skip traces were withdrawn (Codex review round 6).
    cancelled = (await db.execute(
        update(Job)
        .where(
            Job.id == job_id,
            Job.user_id == current_user.id,
            Job.status.in_(CANCELLABLE_STATUSES),
        )
        .values(status="cancelled", finished_at=func.now())
        .returning(Job.id)
    )).first()
    if cancelled is not None:
        return
    current = (await db.execute(
        select(Job.status).where(Job.id == job_id, Job.user_id == current_user.id)
    )).scalar_one_or_none()
    if current is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Cannot cancel a job in '{current}' status",
    )


@router.get("/{job_id}/results", response_model=ResultsPage)
async def get_results(
    job_id: str,
    current_user: CurrentUser,
    request: Request,
    db: AsyncSession = Depends(get_rls_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    q: str | None = Query(None, max_length=100),
    # Phase 4: tax-delinquent view filters (amount owed + months delinquent).
    # VIEW filter only — narrows what's shown, does not change scraping/billing.
    # Set filters exclude rows without structured tax data (every non-King-tax
    # row), since NULL never satisfies the comparison.
    min_amount: float | None = Query(None, ge=0, le=100_000_000),
    max_amount: float | None = Query(None, ge=0, le=100_000_000),
    # Bounded (Codex security): an unbounded months value produces an
    # out-of-int4 bill_year comparison bound -> Postgres "integer out of range"
    # error / log churn. 1200 months = 100y, safely above any real delinquency.
    min_months: int | None = Query(None, ge=0, le=1200),
    max_months: int | None = Query(None, ge=0, le=1200),
    # Phase 5: dialer-ready filter (valid phone + confirmed not-DNC). VIEW filter.
    dialer_ready: bool = Query(False),
    # Tier 0 (057): owner-location filters. None = no filter; True/False match the
    # stored tri-state exactly (unknown/NULL rows excluded from a definite filter).
    absentee: bool | None = Query(None),
    out_of_state: bool | None = Query(None),
    # Allowlisted order of the first column (Date, or Oldest Tax Year on tax jobs).
    # Anything else is a 422, so no caller-supplied column ever reaches ORDER BY.
    sort: ResultsSort = Query(DEFAULT_RESULTS_SORT),
    # Which bucket to list: the run's new leads (default) or the rows an earlier
    # run of this account already delivered. Allowlisted; anything else is a 422.
    category: ResultsCategory = Query(DEFAULT_RESULTS_CATEGORY),
) -> ResultsPage:
    # Rate-limit before the (expensive, multi-query) read to prevent DB-amplification DoS.
    await rate_limit(request, zone="general", identifier=current_user.id)
    result = await db.execute(
        select(Job).where(Job.id == job_id, Job.user_id == current_user.id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    # Look up the scraper config to get the date_range_mode
    config_result = await db.execute(
        select(ScraperConfig).where(
            ScraperConfig.id == job.scraper_config_id,
            ScraperConfig.user_id == current_user.id,  # defense-in-depth owner filter
        )
    )
    config = config_result.scalar_one_or_none()
    from typing import cast

    from src.api.schemas import ScheduleConfigDict
    schedule: ScheduleConfigDict = cast(
        ScheduleConfigDict, (config.schedule or {}) if config else {}
    )
    date_range_mode = schedule.get("date_range_mode") or schedule.get("range_mode", "rolling_90")  # type: ignore[call-overload]  # legacy "range_mode" alias

    safe_q = sanitize_search(q)

    # Standing rule (owner, 2026-09-04): a duplicate is NEVER shown. It was already
    # delivered — and paid for — on an earlier run, so listing it again produced a
    # page of rows for a job the list, the email, the webhook and the bill all
    # reported as zero. The rows stay in `results` as dedup bookkeeping and are still
    # counted in `duplicate_count`/`total_scraped`, which is what the "all N were
    # duplicates" banner is built from — so the run is explained without shipping the
    # rows. Previously they were listed after the new leads and greyed.
    #
    # Per-job delivery ONLY. Lists/segments (src/api/routes/segments.py) and the batch
    # combined export deliberately KEEP duplicates: there, a lead whose only
    # contactable row happens to be a duplicate must not disappear.
    #
    # Owner, 2026-09-17: "227 already delivered" was a number nobody could check.
    # The default view is unchanged (new leads only, never mixed with duplicates);
    # ?category=already_delivered lists the prior-run duplicates on their OWN, with
    # every other rule below (actionable, tax cap, search, filters, sort, paging)
    # applied identically. Reading them is a plain SELECT: nothing here bills,
    # counts against quota or queues a skip trace.
    base_query = select(Result).where(
        Result.job_id == job_id,
        Result.user_id == current_user.id,
        category_condition(category),
    )
    if not _run_delivered(job):
        # N-03: rows exist from `saving` on, but the quota reservation marks the
        # over-allowance ones only after enrichment, and billing happens only at
        # `done`. Until then the run has delivered nothing, so it lists nothing.
        # The scrape stats below still describe the run in progress.
        base_query = base_query.where(false())
    if safe_q:
        pattern = f"%{safe_q}%"
        base_query = base_query.where(
            Result.party_name.ilike(pattern, escape="\\")
            | Result.parcel_id.ilike(pattern, escape="\\")
            | Result.property_address.ilike(pattern, escape="\\")
        )

    # Phase 4: tax filters (amount owed / months delinquent). Applied to the
    # paginated view query so `total` + `items` reflect the filter; the job-level
    # scrape stats below (enriched/parcel/dedup counts) intentionally stay
    # unfiltered (they describe the scrape, not the current filter view).
    from datetime import UTC, datetime, timedelta
    today = datetime.now(UTC).date()
    tax_conditions = build_tax_conditions(
        min_amount, max_amount, min_months, max_months, today
    )
    for cond in tax_conditions:
        base_query = base_query.where(cond)

    # Hard product cap: tax-delinquent rows whose OLDEST unpaid year is more than
    # 18 months old are NEVER shown/counted, regardless of the optional amount/
    # months filters above. Self-scoping (NULL bill_year rows pass), so it's safe
    # on every record type. NOT part of `tax_conditions` because that variable
    # gates the empty-scrape "previous job" hint below — the cap is a standing
    # rule, not a user-set view filter, so it must not change that branch.
    base_query = base_query.where(tax_cap_condition(today))
    # Standing product rule (owner, 2026-09-02): a row with no property AND no
    # mailing address is not a lead — not listed, exported, counted or billed.
    # Kept in `results` for dedup/health only. See src/api/lead_actionability.py.
    base_query = base_query.where(actionable_condition())

    # Tier 0 (057): owner-location filters (absentee / out-of-state). Same view
    # semantics as the tax filters — narrows total + items, leaves scrape stats.
    for cond in build_owner_conditions(absentee, out_of_state):
        base_query = base_query.where(cond)

    # Phase 5: dialer-ready filter (valid phone + not known-DNC). Uses
    # include_unknown_dnc=True because skip-trace leaves phone_dnc_flag NULL
    # (no DNC feed); a strict IS-FALSE would hide every skip-traced phone. The
    # dialer does the authoritative DNC scrub.
    if dialer_ready:
        for cond in dialer_ready_conditions(include_unknown_dnc=True):
            base_query = base_query.where(cond)

    count_result = await db.execute(
        select(func.count()).select_from(base_query.subquery())
    )
    total = count_result.scalar_one()

    rows_result = await db.execute(
        # Sorted over the whole filtered set BEFORE offset/limit (see results_sort).
        base_query.order_by(*results_order_by(config.record_type if config else None, sort))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [ResultRow.model_validate(r) for r in rows_result.scalars().all()]
    if category == "already_delivered" and items:
        await _attach_delivery_provenance(db, current_user.id, items, today)

    # Count enriched records (have real property_address), excluding duplicates
    enriched_result = await db.execute(
        select(func.count()).where(
            Result.job_id == job_id,
            Result.user_id == current_user.id,
            Result.is_duplicate.is_(False),
            Result.property_address.isnot(None),
            Result.property_address != "",
            Result.property_address != "(enrichment unavailable)",
        )
    )
    enriched_count = enriched_result.scalar_one()

    # Check if enrichment is still running:
    # It's running if parcels exist without addresses AND the enrichment
    # task hasn't finished yet (no "Enrichment complete" log entry).
    parcel_count_result = await db.execute(
        select(func.count()).where(
            Result.job_id == job_id,
            Result.user_id == current_user.id,
            func.length(Result.parcel_id) >= 10,
        )
    )
    parcel_count = parcel_count_result.scalar_one()

    enrichment_done_result = await db.execute(
        select(func.count()).where(
            JobLog.job_id == job_id,
            JobLog.message.like("Enrichment complete%"),
        )
    )
    enrichment_task_finished = enrichment_done_result.scalar_one() > 0

    # Also check the "No records" log — enrichment skipped
    if not enrichment_task_finished:
        skip_result = await db.execute(
            select(func.count()).where(
                JobLog.job_id == job_id,
                JobLog.message.like("No records with parcel%"),
            )
        )
        enrichment_task_finished = skip_result.scalar_one() > 0

    # A terminal job cannot still be enriching: inline enrichment runs before the job
    # leaves `enriching`. Matching log text alone missed every completion line added
    # later ("Address enrichment partly complete...", "Address enrichment failed..."),
    # so a job with deferred mailing lookups reported enriching=true forever and the
    # results page polled every 5 seconds indefinitely. Background mailing recovery is
    # surfaced per row (enrichment_data.mailing_lookup_deferred), not by this flag.
    if job.status in {"done", "failed", "cancelled"}:
        enrichment_task_finished = True

    enriching = parcel_count > 0 and not enrichment_task_finished

    # Total scraped (including duplicates) and duplicate count — both scoped to
    # ACTIONABLE rows so the "all N records were duplicates" banner can never be
    # driven by rows that are not leads (Codex).
    # ONE aggregate, not four (Codex). These counts explain each other on the
    # page: the banner renders `duplicate_count`, and the UI derives
    # "duplicates from an earlier run" as duplicate_count - same_run_count.
    # Read under separate READ COMMITTED snapshots, a finalize committing
    # between two of them could return a same_run_count larger than the
    # duplicate_count taken moments earlier, and the UI would render a negative
    # number. One statement, one snapshot, and the arithmetic cannot go
    # inconsistent no matter what commits alongside it.
    #
    # new_count is deliberately NOT tax-capped, matching workers/tasks.py's
    # billable_count exactly — it must track jobs.record_count, which is what the
    # list, the email and the webhook all report, not `total`.
    #
    # A 'superseded' row is left out of every count here. It held the claim on
    # this run without ever being delivered, and a LATER run took the claim and
    # delivered the lead (transfer_undelivered_claims). From this run's page it is
    # neither new nor "already delivered", and counting it as a duplicate would
    # name a source run newer than this one. It only becomes actionable here if a
    # backfill fills its address after the handover, which is exactly when a
    # count would start telling the reader something false.
    counts_row = (await db.execute(
        select(
            func.count().label("total_scraped"),
            func.count().filter(Result.is_duplicate.is_(True)).label("duplicates"),
            func.count().filter(Result.is_duplicate.is_(False)).label("new_leads"),
            func.count()
            .filter(
                Result.is_duplicate.is_(True),
                Result.duplicate_reason == "same_run",
            )
            .label("same_run"),
            # The tab number. Same predicate as the already_delivered list's base
            # query before view filters, INCLUDING the tax cap that the list applies
            # and new_leads (a billing mirror) deliberately does not.
            func.count()
            .filter(already_delivered_condition(), tax_cap_condition(today))
            .label("already_delivered"),
            # Skip-trace state of exactly those rows (AlreadyDeliveredContacts). A lead
            # already delivered can still be looked up later, and the tab says so.
            # Every bucket is counted from its own predicate and they are disjoint;
            # `unknown` is the only remainder, so a status this code does not know is
            # never reported as "not looked up" (2e).
            *(
                func.count()
                .filter(already_delivered_condition(), tax_cap_condition(today), condition)
                .label(f"delivered_{bucket}")
                for bucket, condition in _CONTACT_BUCKETS
            ),
            # Of the answered ones, those copied from an earlier answer (no lookup bought).
            func.count()
            .filter(already_delivered_condition(), tax_cap_condition(today),
                    Result.skip_trace_status.in_(("hit", "miss")),
                    Result.skip_trace_source == "reused")
            .label("delivered_reused"),
        ).where(
            Result.job_id == job_id,
            Result.user_id == current_user.id,
            actionable_condition(),
            func.coalesce(Result.duplicate_reason, "") != "superseded",
        )
    )).one()
    total_scraped = counts_row.total_scraped
    duplicate_count = counts_row.duplicates
    new_count = counts_row.new_leads
    same_run_duplicate_count = counts_row.same_run
    already_delivered_count = counts_row.already_delivered
    delivered_buckets = {
        bucket: getattr(counts_row, f"delivered_{bucket}") for bucket, _ in _CONTACT_BUCKETS
    }
    already_delivered_contacts = AlreadyDeliveredContacts(
        **delivered_buckets,
        unknown=already_delivered_count - sum(delivered_buckets.values()),
        reused=counts_row.delivered_reused,
    )

    # ── Where this job's duplicates came from (migration 089) ───────────────
    # Read off results.duplicate_source_* — stamped by the worker at the moment
    # each row was classified. NOT a live join against delivered_records: that
    # table is worker-only (bridgeleads_app holds no privilege on it and
    # provision_rls_roles.sql hard-fails if it ever does), its claims are
    # released and re-claimed so a read-time join answers "who holds this now"
    # rather than "who held it then", and 82% of its production rows already
    # point at a purged job.
    #
    # Scoped to the SAME actionable predicate as duplicate_count above, so the
    # groups sum to the number the banner renders instead of disagreeing with it.
    # Rows stamped before 089 have a NULL source and land in `unattributed`.
    dup_source_rows = await db.execute(
        select(
            Result.duplicate_source_job_id,
            func.max(Result.duplicate_source_at).label("run_at"),
            func.count().label("n"),
        )
        .where(
            Result.job_id == job_id,
            Result.user_id == current_user.id,
            Result.is_duplicate.is_(True),
            actionable_condition(),
            # The same buckets as the counts above: only prior deliveries.
            func.coalesce(Result.duplicate_reason, "prior_run") == "prior_run",
        )
        .group_by(Result.duplicate_source_job_id)
        .order_by(func.count().desc())
    )
    grouped = dup_source_rows.all()
    unattributed_duplicate_count = sum(
        g.n for g in grouped if g.duplicate_source_job_id is None
    )
    named = [g for g in grouped if g.duplicate_source_job_id is not None]

    # A source job may have been purged. Confirm each still exists, still belongs
    # to this user, and actually FINISHED before offering a link to it. The id is
    # stamped without a foreign key on purpose, so a dangling pointer is expected
    # rather than exceptional. The `done` check is separate and load-bearing: a
    # claim is written BEFORE its job completes, so a run that crashed after
    # claiming (and then released those claims) must never be presented as the
    # run that delivered these leads.
    #
    # One batched query, not one per group: the group count is bounded by this
    # user's prior runs of this scraper, which is small but not fixed, and an
    # unbounded per-group round trip on a read path is how a results page starts
    # timing out for the heaviest accounts.
    linkable: set[str] = set()
    if named:
        avail = await db.execute(
            select(Job.id).where(
                Job.id.in_([g.duplicate_source_job_id for g in named]),
                Job.user_id == current_user.id,
                Job.status == "done",
            )
        )
        linkable = {str(j) for j in avail.scalars().all()}

    duplicate_sources = [
        DuplicateSource(
            job_id=g.duplicate_source_job_id,
            run_at=g.run_at,
            duplicate_count=g.n,
            job_available=str(g.duplicate_source_job_id) in linkable,
        )
        for g in named
    ]


    # When results are empty (all duplicates or no new leads), find
    # the most recent previous job for the same county/record_type
    # that has actual Result rows, so the user can navigate there.
    # Searches across ALL scraper configs for the same county+type,
    # not just the same config_id.
    previous_job_id = None
    previous_job_run_at = None
    # Skip the empty-scrape "previous job" suggestion when ANY view filter is
    # active: total==0 then means "no rows matched the filter", NOT "the job
    # scraped nothing", and the prior job wasn't checked against the same filter
    # so suggesting it would be misleading (Codex).
    if (
        total == 0
        # Only the new-leads view explains an empty page this way. An empty
        # already-delivered view just means this run re-found nothing old.
        and category == "new"
        and config
        and not tax_conditions
        and not dialer_ready
        # owner-location filters were missed here (Codex): with one active, total==0
        # means "nothing matched the filter", so pointing at a previous job — which
        # was never checked against that filter — is just as misleading as it is for
        # the tax/dialer filters this already guards.
        and not build_owner_conditions(absentee, out_of_state)
    ):
        # Find all config IDs for same county/state/record_type
        sibling_configs = await db.execute(
            select(ScraperConfig.id).where(
                func.lower(ScraperConfig.county) == config.county.lower(),
                func.upper(ScraperConfig.state) == config.state.upper(),
                ScraperConfig.record_type == config.record_type,
                ScraperConfig.user_id == current_user.id,
            )
        )
        sibling_ids = list(sibling_configs.scalars().all())

        if sibling_ids:
            # Find most recent done job across all sibling configs
            # that has at least 1 non-duplicate Result row
            from sqlalchemy import exists
            prev_result = await db.execute(
                select(Job.id, Job.created_at)
                .where(
                    Job.scraper_config_id.in_(sibling_ids),
                    Job.user_id == current_user.id,
                    Job.id != job_id,
                    Job.status == "done",
                    # The link is labelled "View previous results". Without this
                    # bound it ordered by created_at DESC across ALL sibling
                    # jobs, so opening an OLD all-duplicate run linked to the
                    # NEWEST run — in production, a run two months LATER that
                    # delivered none of the leads being explained. The page
                    # asserted "you already received these" and then offered a
                    # link that appeared to disprove it, which is how a correct
                    # duplicate classification was reported as a cross-tenant
                    # leak (2026-09-08). Previous means previous.
                    Job.created_at < job.created_at,
                    exists(
                        select(Result.id).where(
                            Result.job_id == Job.id,
                            Result.user_id == current_user.id,
                            Result.is_duplicate.is_(False),
                            # Only a prior job with VISIBLE leads is worth linking
                            # to — same standing rules as the list (Codex).
                            tax_cap_condition(today),
                            actionable_condition(),
                        )
                    ),
                )
                .order_by(Job.created_at.desc())
                .limit(1)
            )
            prev_row = prev_result.first()
            if prev_row:
                previous_job_id = prev_row.id
                # Dated so the banner can name the run instead of saying
                # only "previous", which is what left the reader with no
                # way to check the claim.
                previous_job_run_at = prev_row.created_at

    # NTS Tier 1: show the Auction Date / Default Owed columns for EVERY
    # pre_foreclosure job (user pref: consistent columns across scrapes — the cells
    # read '—' where a lead has no matched trustee sale, rather than the whole columns
    # vanishing on a job that happened to match zero). trustee_sale (Auction Leads) is
    # sourced FROM the NTS cache, so every row has auction data — the record type IS
    # the rule for both; other types keep the row probe (defensive — nothing else
    # populates auction today). Job-wide, not page-scoped, so the columns don't
    # flicker by page when matches are sparse (Codex).
    if config is not None and config.record_type in ("pre_foreclosure", "trustee_sale"):
        has_auction_data = True
    else:
        auction_probe = await db.execute(
            select(Result.id)
            .where(
                Result.job_id == job_id,
                Result.user_id == current_user.id,
                Result.auction_date.isnot(None),
            )
            .limit(1)
        )
        has_auction_data = auction_probe.scalar_one_or_none() is not None

    # Why those columns look the way they do. Only meaningful for pre_foreclosure:
    # trustee_sale rows are sourced FROM the notice cache so they always carry a sale
    # date, and no other record type has auction data at all. Computed from each
    # lead's own recording date rather than a stored marker, so a run that predates
    # the missing-reason stamping still reports correctly.
    auction_coverage = None
    if config is not None and config.record_type == "pre_foreclosure":
        pub_cutoff = today - timedelta(days=AUCTION_PUBLICATION_LAG_DAYS)
        cov = (await db.execute(
            select(
                func.count().filter(Result.auction_date.isnot(None)).label("matched"),
                func.count().filter(
                    Result.auction_date.is_(None),
                    Result.date_recorded_parsed.isnot(None),
                    Result.date_recorded_parsed > pub_cutoff,
                ).label("awaiting"),
                func.count().filter(
                    Result.auction_date.is_(None),
                    or_(
                        Result.date_recorded_parsed.is_(None),
                        Result.date_recorded_parsed <= pub_cutoff,
                    ),
                ).label("no_notice"),
            ).where(Result.job_id == job_id, Result.user_id == current_user.id)
        )).first()
        auction_coverage = AuctionCoverage(
            matched=cov.matched or 0,
            awaiting_publication=cov.awaiting or 0,
            no_notice_found=cov.no_notice or 0,
        )

    # The run-count breakdown: the worker's done-time snapshot, else the same
    # partition read now (one aggregate, scoped by job AND user on this RLS session).
    # Only for a finished run: before `done`, records_found is written ahead of the
    # filter and the saves, so rows still on their way would read as "not saved".
    # A REJECTED stored snapshot shows nothing: never the live partition in its place.
    breakdown, rejected = _snapshot_breakdown(job)
    breakdown_basis = "snapshot" if breakdown is not None else None
    if breakdown is None and not rejected and job.status == "done":
        live, _why = live_breakdown(
            await read_partition_async(db, job_id, current_user.id),
            status=job.status,
            records_found=job.records_found,
            retry_count=job.retry_count,
        )
        if live is not None:
            breakdown, breakdown_basis = RunBreakdown(**live), "live"

    return ResultsPage(
        job_id=job_id, total=total, page=page, page_size=page_size,
        items=items, enriched_count=enriched_count, enriching=enriching,
        total_scraped=total_scraped, duplicate_count=duplicate_count,
        new_count=new_count,
        date_range_mode=date_range_mode,
        previous_job_id=previous_job_id,
        previous_job_run_at=previous_job_run_at,
        duplicate_sources=duplicate_sources,
        unattributed_duplicate_count=unattributed_duplicate_count,
        same_run_duplicate_count=same_run_duplicate_count,
        already_delivered_count=already_delivered_count,
        already_delivered_contacts=already_delivered_contacts,
        has_auction_data=has_auction_data,
        auction_coverage=auction_coverage,
        breakdown=breakdown,
        breakdown_basis=breakdown_basis,
    )


async def _attach_delivery_provenance(
    db: AsyncSession, user_id: str, items: list[ResultRow], today
) -> None:
    """Say, per already-delivered row on THIS page, what can be checked about it.

    At most three batched queries for the whole page, never one per row:
      1. which source runs still exist, belong to this account and finished. A
         source id is stamped with no foreign key, so a purged run is expected;
         `done` matters because a claim is written before its run completes.
      2. the page rows' dedup keys (not part of ResultRow), only if any run is left.
      3. which of those runs still LIST the same property as a new lead (same key,
         not a duplicate, actionable, inside the tax cap), i.e. the original the
         user can actually open and see.
    Both are scoped to `user_id` explicitly as well as by RLS, so a source id that
    somehow named another account's run reads as unavailable and never confirms
    that run exists.
    """
    source_ids = {r.duplicate_source_job_id for r in items if r.duplicate_source_job_id}
    available: set[str] = set()
    if source_ids:
        found = await db.execute(
            select(Job.id).where(
                Job.id.in_(source_ids),
                Job.user_id == user_id,
                Job.status == "done",
            )
        )
        available = {str(j) for j in found.scalars().all()}

    hashes: dict[str, str | None] = {}
    visible: set[tuple[str, str]] = set()
    if available:
        # dedup_hash is not on ResultRow (an internal key), so read it for the
        # page's ids in the same round trip as the originals it is matched against.
        page_hashes = await db.execute(
            select(Result.id, Result.dedup_hash).where(
                Result.id.in_([r.id for r in items]),
                Result.user_id == user_id,
            )
        )
        hashes = {str(row.id): row.dedup_hash for row in page_hashes}
        wanted = {h for h in hashes.values() if h}
        if wanted:
            originals = await db.execute(
                select(Result.job_id, Result.dedup_hash).where(
                    Result.user_id == user_id,
                    Result.job_id.in_(available),
                    Result.dedup_hash.in_(wanted),
                    Result.is_duplicate.is_(False),
                    actionable_condition(),
                    tax_cap_condition(today),
                ).distinct()
            )
            visible = {(str(o.job_id), o.dedup_hash) for o in originals}

    for r in items:
        src = r.duplicate_source_job_id
        r.duplicate_source_available = src in available if src else False
        r.duplicate_original_visible = bool(
            src and src in available and (src, hashes.get(r.id)) in visible
        )
        # Echo a run id only when this account can open it. A purged run's id is
        # useless to the page, and an id that ever named another account's run
        # (0 in production, 2026-09-17) must not be handed back at all. The claim
        # date stays: it is this row's own history.
        if not r.duplicate_source_available:
            r.duplicate_source_job_id = None


# ─── Contact lookups: the quote (Phase 1b-1c) ────────────────────────────────
#
# Contract: tasks/todo-lookup-contacts.md, "FINAL 1b-1c contract and build list".
# Spends nothing and writes no database row: it plans the tab with the shared
# planner, reads the pause state, and stores ONE Redis key the confirm (1b-2) reads.

_QUOTE_TTL_SECONDS = 600
# Every Redis call here is bounded end to end: the socket timeouts stop a thread,
# and the await is bounded again in case the pool itself stalls.
_LOOKUP_REDIS_SOCKET_TIMEOUT_S = 0.5
_LOOKUP_REDIS_CALL_BOUND_S = 1.0
_lookup_redis_client: _sync_redis.Redis | None = None


def _lookup_redis() -> _sync_redis.Redis:
    """A SYNC client, called only through `_bounded`. `read_pause_state` calls
    `hmget` synchronously, so the API's async clients cannot be handed to it."""
    global _lookup_redis_client
    if _lookup_redis_client is None:
        _lookup_redis_client = _sync_redis.from_url(
            settings.REDIS_URL, **settings.redis_kwargs(),
            socket_timeout=_LOOKUP_REDIS_SOCKET_TIMEOUT_S,
            socket_connect_timeout=_LOOKUP_REDIS_SOCKET_TIMEOUT_S,
        )
    return _lookup_redis_client


async def _bounded(fn, *args, **kwargs):
    return await asyncio.wait_for(
        run_in_threadpool(functools.partial(fn, *args, **kwargs)),
        _LOOKUP_REDIS_CALL_BOUND_S,
    )


def _quote_key(user_id: str, job_id: str, category: str) -> str:
    """ONE live quote per tab: a new quote replaces the previous one."""
    return f"bridgeleads:contact_lookup:quote:v2:{user_id}:{job_id}:{category}"


def _lookups_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "contact_lookups_unavailable",
            "message": "Contact lookups are unavailable right now. Nothing was charged; "
                       "please try again in a few minutes.",
        },
    )


@router.post(
    "/{job_id}/contact-lookups/quote",
    response_model=ContactLookupQuote,
    responses={
        **RUN_START_402_RESPONSES,
        503: {"model": ContactLookupUnavailableResponse,
              "description": "Lookups are switched off or a dependency is unreachable."},
    },
)
async def quote_contact_lookups(
    job_id: str,
    body: ContactLookupQuoteRequest,
    current_user: CurrentUser,
    request: Request,
    db: AsyncSession = Depends(get_rls_db),
) -> ContactLookupQuote:
    """Quote a "look up contacts" action for one results tab. Non-binding."""
    now = datetime.now(UTC)
    today = now.date()  # one clock for every query below
    # The limiter's own async client has no socket timeout: bound it, so a stalled
    # Redis refuses here instead of hanging the request (consult r4, R5).
    try:
        await asyncio.wait_for(
            rate_limit(request, zone="lookup_quote", identifier=current_user.id),
            _LOOKUP_REDIS_CALL_BOUND_S,
        )
    except TimeoutError:
        raise _lookups_unavailable() from None

    job = (await db.execute(
        select(Job).where(Job.id == job_id, Job.user_id == current_user.id)
    )).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if not _run_delivered(job):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "run_not_finished",
                "message": "This run has not finished, so there are no leads to look up yet.",
            },
        )

    plan = normalize_plan(current_user.plan)
    if plan not in SKIP_TRACE_ADDON_PLANS:
        raise_plan_features([skip_trace_violation(plan)])
    # A frozen or ended account may not start billable work. `over_limit` is the
    # RECORD allowance, and a lookup never counts as a record, so it does not refuse.
    eligibility = run_eligibility(current_user, now)
    if eligibility.code in ("frozen", "ended"):
        raise run_refusal_http(eligibility.code, eligibility.message, eligibility.resumes_at)
    if not settings.SKIP_TRACE_ENABLED or not settings.TRACERFY_API_TOKEN:
        raise _lookups_unavailable()
    # WHO MAY BUY is the claim's own rule (audit S3-03/S4-01), read here without the
    # claim's row lock: the quote is advisory, and the claim re-reads it locked. The
    # gates above already refused Starter, frozen and ended accounts; a free trial
    # is capped at its WHOLE lifetime allowance, since the credits it already used
    # sit in a worker-only table. The claim's room is the allowance minus those, so
    # this stays an upper bound (owner decision, 2026-09-28).
    from src.workers.skip_trace_claim import ACCESS_FULL, ACCESS_TRIAL, paid_lookup_access

    access = paid_lookup_access(current_user, now)
    if access not in (ACCESS_FULL, ACCESS_TRIAL):
        raise _lookups_unavailable()  # unreachable after the gates above; fail closed
    credit_cap = settings.SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE if access == ACCESS_TRIAL else None

    # Redis BEFORE the scan: a quote nobody can store is never computed (R4).
    try:
        r = _lookup_redis()
        await _bounded(r.ping)
    except Exception:  # noqa: BLE001 - any Redis failure is the same 503
        raise _lookups_unavailable() from None

    # Imported HERE, after the cheap gates, not at module top: the planner imports
    # scraper modules (pierce_atip_owner -> base_scraper), and base_scraper imports
    # src.api, whose package __init__ imports this router. A module-level import
    # closed that loop, so any process that imported `src.scrapers` before `src.api`
    # (every ops script) died with a circular ImportError (#393). Pinned by
    # tests/test_import_cycles.py.
    from src.api.contact_lookup_planner import (
        ALREADY_ANSWERED,
        EXCLUDED_BUCKETS,
        IN_PROGRESS,
        PLANNER_VERSION,
        PREVIOUSLY_ATTEMPTED,
        count_remaining,
        plan_tab_window,
        policy_from_settings,
        status_buckets,
        tab_status_counts,
    )

    user_id = str(current_user.id)
    policy = policy_from_settings()
    buckets = status_buckets(await tab_status_counts(db, job_id, user_id, body.category, today))
    window = await plan_tab_window(db, job_id, user_id, body.category, today, policy,
                                   credit_cap=credit_cap)
    remaining = await count_remaining(db, job_id, user_id, body.category, today, window)
    truncated = window.stopped is not None and remaining > 0

    try:
        pause = await _bounded(read_pause_state, r, user_id, now)
    except Exception:  # noqa: BLE001 - the contract: unreadable is UNKNOWN
        pause = PauseState(PAUSE_UNKNOWN)

    unit_cents = unit_price_cents(plan)
    if unit_cents is None:  # every add-on plan has a price; never quote without one
        raise _lookups_unavailable()
    included = included_lookups_remaining(current_user, now)
    quote_id = secrets.token_urlsafe(32)
    expires_at = now + timedelta(seconds=_QUOTE_TTL_SECONDS)
    payload = {
        "v": 2,
        "quote_id": quote_id,
        "user_id": user_id,
        "job_id": job_id,
        "category": body.category,
        "quoted_ids": window.quoted_ids,
        "advanced_count": window.advanced_count,
        "quoted_credits": window.quoted_credits,
        "access": access,
        "trial_credit_allowance": credit_cap,
        "over_credit_cap": window.over_credit_cap,
        "counts": {**window.counts, **buckets},
        "examined": window.examined,
        "window_end": (None if window.window_end is None
                       else [window.window_end[0].isoformat(), window.window_end[1]]),
        "stopped": window.stopped,
        "remaining": remaining,
        "planner_version": PLANNER_VERSION,
        "policy": {"pierce_cv_owner_skip_trace_enabled":
                   policy.pierce_cv_owner_skip_trace_enabled},
        "unit_price_cents": unit_cents,
        "currency": LOOKUP_CURRENCY,
        "pricing_version": LOOKUP_PRICING_VERSION,
        "included_remaining_at_quote": included,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    try:
        await _bounded(r.set, _quote_key(user_id, job_id, body.category),
                       json.dumps(payload), ex=_QUOTE_TTL_SECONDS)
    except Exception:  # noqa: BLE001 - a quote nobody can confirm is never shown
        raise _lookups_unavailable() from None

    return ContactLookupQuote(
        quote_id=quote_id,
        expires_at=expires_at,
        category=body.category,
        max_new_lookups=len(window.quoted_ids),
        advanced_count=window.advanced_count,
        examined=window.examined,
        truncated=truncated,
        truncated_reason=window.stopped if truncated else None,
        excluded=ContactLookupExcluded(**{b: window.counts[b] for b in EXCLUDED_BUCKETS}),
        already_answered=buckets[ALREADY_ANSWERED],
        in_progress=buckets[IN_PROGRESS],
        previously_attempted=buckets[PREVIOUSLY_ATTEMPTED],
        remaining=remaining,
        access=access,
        trial_credit_allowance=credit_cap,
        over_trial_allowance=window.over_credit_cap,
        included_lookups_remaining=included,
        unit_price_cents=unit_cents,
        currency=LOOKUP_CURRENCY,
        pause=ContactLookupPause(
            status=pause.status,
            normal_resume_at=pause.normal_resume_at,
            advanced_resume_at=pause.advanced_resume_at,
        ),
    )


# ─── Contact lookups: the confirm (Phase 1b-2d) ──────────────────────────────
#
# Contract: tasks/todo-lookup-contacts.md, "## Phase 1b-2d — the confirm endpoint" as
# amended by AO1-AO5. The switch that makes contact lookup purchasable, and it buys
# nothing itself: it commits a durable `dispatching` action with one `quoted` row per
# lead the quote offered, then publishes the worker (`lookup_contacts`, 2b), which
# claims them through the one claim path. A lost publish is re-driven by the
# reconciler (2c, P3); an action nobody starts expires at its 30-minute deadline.

# A broker stall must hold neither the event loop nor the request threads (AO3): a
# publish runs on its own two threads, behind a slot it takes WITHOUT waiting. With
# both busy it is skipped, and the reconciler publishes the action instead.
_PUBLISH_BOUND_S = 3.0
_publish_slots = threading.BoundedSemaphore(2)
_publish_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="contact-lookup-publish",
)

# Delete the tab's quote only if it is still the one just confirmed: a newer quote
# written in between belongs to the customer's next action.
_DELETE_IF_SAME_QUOTE = """
local v = redis.call('GET', KEYS[1])
if v and cjson.decode(v)['quote_id'] == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


_PG_INT_MAX = 2_147_483_647  # contact_lookup_actions.unit_price_cents is INTEGER
_QUOTE_STOPS = (None, "cap", "credit_cap", "scan_limit")  # the planner's window.stopped


def _is_count(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _valid_quote_payload(quote: dict):
    """(expires_at, unique quoted ids, truncated) when the stored quote is complete and
    well-formed, else None. A corrupt payload is refused as unsupported, never a 500
    (2d reviews r1, r2)."""
    try:
        expires_at = datetime.fromisoformat(quote["expires_at"])
        if expires_at.tzinfo is None:
            return None
        raw_ids = quote["quoted_ids"]
        if not isinstance(raw_ids, list):
            return None
        ids = list(dict.fromkeys(str(uuid.UUID(str(i))) for i in raw_ids))
        price, cur, pv = (quote["unit_price_cents"], quote["currency"],
                          quote["pricing_version"])
        stopped, remaining = quote["stopped"], quote["remaining"]
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if not (_is_count(price) and 0 < price <= _PG_INT_MAX
            and isinstance(cur, str) and len(cur) == 3
            and isinstance(pv, str) and 0 < len(pv) <= 32
            and stopped in _QUOTE_STOPS and _is_count(remaining)):
        return None
    return expires_at, ids, stopped is not None and remaining > 0


def _confirm_refusal(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _quote_expired() -> HTTPException:
    return _confirm_refusal(
        status.HTTP_410_GONE, "quote_expired",
        "This quote has expired or was replaced by a newer one. Nothing was charged; "
        "please get a new quote.",
    )


async def _action_for_quote(db: AsyncSession, quote_id: str, user_id: str):
    """The action a quote already became, for THIS account only."""
    return (await db.execute(
        text("SELECT id::text AS id, job_id::text AS job_id, category, status, "
             "       quoted_count, truncated "
             "FROM contact_lookup_actions "
             "WHERE quote_id = :q AND user_id = CAST(:u AS uuid)"),
        {"q": quote_id, "u": user_id},
    )).first()


def _replayed(row, job_id: str, category: str) -> ContactLookupAction:
    """A confirm already made answers with its action, whatever changed since (AO1)."""
    if row.job_id != job_id or row.category != category:
        raise _confirm_refusal(
            status.HTTP_409_CONFLICT, "quote_mismatch",
            "This quote was confirmed for a different results tab.",
        )
    return ContactLookupAction(action_id=row.id, status=row.status,
                               quoted_count=row.quoted_count, truncated=row.truncated)


def _publish_blocking(task, action_id: str) -> None:
    try:
        # retry=False: kombu's publish-retry loop would hold this thread (AO3).
        task.apply_async(args=[action_id], retry=False)
    finally:
        _publish_slots.release()


async def _publish_contact_lookup(task, action_id: str) -> bool:
    """True when the worker's message was handed to the broker. Never raises."""
    if not _publish_slots.acquire(blocking=False):
        _logger.warning("contact lookup %s: publish skipped (broker busy); the "
                        "reconciler will publish it", action_id)
        return False
    try:
        future = _publish_pool.submit(_publish_blocking, task, action_id)
    except Exception:  # noqa: BLE001 - the action is committed; P3 re-drives it
        _publish_slots.release()
        _logger.warning("contact lookup %s: publish not started", action_id, exc_info=True)
        return False
    try:
        await asyncio.wait_for(asyncio.wrap_future(future), _PUBLISH_BOUND_S)
        return True
    except Exception:  # noqa: BLE001 - incl. the timeout; the thread frees its slot
        _logger.warning("contact lookup %s: publish failed; the reconciler will "
                        "publish it", action_id, exc_info=True)
        return False


@router.post(
    "/{job_id}/contact-lookups",
    response_model=ContactLookupAction,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        **RUN_START_402_RESPONSES,
        404: {"description": "No such run for this account."},
        429: {"description": "Too many writes: 30 per minute per account."},
        409: {"model": ContactLookupConfirmErrorResponse,
              "description": "The quote is stale, empty, unsupported or for another tab, "
                             "or the run has not finished."},
        410: {"model": ContactLookupConfirmErrorResponse,
              "description": "The quote expired or was replaced. Nothing was charged."},
        503: {"model": ContactLookupUnavailableResponse,
              "description": "Lookups are switched off or a dependency is unreachable."},
    },
)
async def confirm_contact_lookups(
    job_id: str,
    body: ContactLookupConfirmRequest,
    current_user: CurrentUser,
    request: Request,
    db: AsyncSession = Depends(get_rls_db),
) -> ContactLookupAction:
    """Buy the lookups a quote offered. Idempotent: the same quote returns the same
    action. Accepted (202): the lookups are queued by a worker shortly after."""
    now = datetime.now(UTC)
    # The `writes` zone (V7/W4), 30/min. A Redis failure falls back to the
    # per-process limiter; only a stalled limiter call is refused here (AO4).
    try:
        await asyncio.wait_for(
            rate_limit(request, zone="writes", identifier=current_user.id),
            _LOOKUP_REDIS_CALL_BOUND_S,
        )
    except TimeoutError:
        raise _lookups_unavailable() from None

    job = (await db.execute(
        select(Job).where(Job.id == job_id, Job.user_id == current_user.id)
    )).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if not _run_delivered(job):
        raise _confirm_refusal(
            status.HTTP_409_CONFLICT, "run_not_finished",
            "This run has not finished, so there are no leads to look up yet.",
        )
    user_id = str(current_user.id)

    # A replay first, before anything that can change after a purchase (AO1).
    existing = await _action_for_quote(db, body.quote_id, user_id)
    if existing is not None:
        return _replayed(existing, job_id, body.category)

    # The same gates as the quote, now that this would create something.
    plan = normalize_plan(current_user.plan)
    if plan not in SKIP_TRACE_ADDON_PLANS:
        raise_plan_features([skip_trace_violation(plan)])
    eligibility = run_eligibility(current_user, now)
    if eligibility.code in ("frozen", "ended"):
        raise run_refusal_http(eligibility.code, eligibility.message, eligibility.resumes_at)
    if not settings.SKIP_TRACE_ENABLED or not settings.TRACERFY_API_TOKEN:
        raise _lookups_unavailable()
    from src.workers.skip_trace_claim import ACCESS_FULL, ACCESS_TRIAL, paid_lookup_access

    if paid_lookup_access(current_user, now) not in (ACCESS_FULL, ACCESS_TRIAL):
        raise _lookups_unavailable()  # fail closed; the claim re-checks it locked

    key = _quote_key(user_id, job_id, body.category)
    try:
        r = _lookup_redis()
        raw = await _bounded(r.get, key)
    except Exception:  # noqa: BLE001 - any Redis failure is the same 503
        raise _lookups_unavailable() from None
    if raw is None:
        raise _quote_expired()
    try:
        quote = json.loads(raw)
    except ValueError:
        quote = {}
    if not isinstance(quote, dict):  # valid JSON, but not a quote object
        quote = {}
    if quote.get("v") != 2:
        raise _confirm_refusal(status.HTTP_409_CONFLICT, "quote_unsupported",
                               "This quote cannot be confirmed. Please get a new quote.")
    if (quote.get("quote_id") != body.quote_id or quote.get("user_id") != user_id
            or quote.get("job_id") != job_id or quote.get("category") != body.category):
        raise _quote_expired()  # superseded by a newer quote of this tab
    valid = _valid_quote_payload(quote)
    if valid is None:
        raise _confirm_refusal(status.HTTP_409_CONFLICT, "quote_unsupported",
                               "This quote cannot be confirmed. Please get a new quote.")
    expires_at, ids, truncated = valid
    if expires_at <= now:
        raise _quote_expired()
    if not ids:
        raise _confirm_refusal(status.HTTP_409_CONFLICT, "nothing_to_look_up",
                               "This quote offered no leads to look up.")

    action_id = str(uuid.uuid4())
    snapshot = {  # what the quote showed, frozen with the action (V6)
        k: quote.get(k) for k in (
            "counts", "examined", "stopped", "remaining", "window_end", "policy",
            "access", "trial_credit_allowance", "over_credit_cap", "planner_version",
            "advanced_count", "quoted_credits", "included_remaining_at_quote",
            "expires_at")
    }
    snapshot["quote_created_at"] = quote.get("created_at")
    params = {"a": action_id, "u": user_id, "j": job_id}
    try:
        # ONE transaction, in the tenant session, so the 101 guards bind: a
        # `dispatching` action, its `quoted` rows, and the one initial event.
        await db.execute(
            text("INSERT INTO contact_lookup_actions (id, user_id, job_id, category, "
                 "  quote_id, status, unit_price_cents, currency, pricing_version, "
                 "  quoted_count, truncated, quote_snapshot) "
                 "VALUES (CAST(:a AS uuid), CAST(:u AS uuid), CAST(:j AS uuid), :c, :q, "
                 "  'dispatching', :price, :cur, :pv, :n, :t, CAST(:snap AS jsonb))"),
            {**params, "c": body.category, "q": body.quote_id,
             "price": quote.get("unit_price_cents"), "cur": quote.get("currency"),
             "pv": quote.get("pricing_version"), "n": len(ids), "t": truncated,
             "snap": json.dumps(snapshot)},
        )
        # The quoted set is PROVEN against this tenant's run (V1): every quoted id
        # must still be one of this account's leads in this run, or nothing is bought.
        inserted = (await db.execute(
            text("INSERT INTO contact_lookup_action_results (id, action_id, user_id, "
                 "  result_id, disposition) "
                 "SELECT gen_random_uuid(), CAST(:a AS uuid), CAST(:u AS uuid), r.id, "
                 "       'quoted' "
                 "FROM results r "
                 "WHERE r.id = ANY(CAST(:ids AS uuid[])) "
                 "  AND r.user_id = CAST(:u AS uuid) AND r.job_id = CAST(:j AS uuid)"),
            {**params, "ids": ids},
        )).rowcount
        if inserted != len(ids):
            await db.rollback()
            raise _confirm_refusal(
                status.HTTP_409_CONFLICT, "quote_stale",
                "Some leads in this quote are no longer available. Nothing was charged; "
                "please get a new quote.",
            )
        await db.execute(
            text("INSERT INTO contact_lookup_action_events (id, action_id, user_id, "
                 "  to_status) "
                 "VALUES (gen_random_uuid(), CAST(:a AS uuid), CAST(:u AS uuid), "
                 "  'dispatching')"),
            params,
        )
        await db.commit()
    except IntegrityError as exc:
        # Only the one-action-per-quote constraint means "confirmed concurrently";
        # anything else is a real error (AO5).
        fields = _integrity_error_fields(exc)
        if fields.get("sqlstate") != "23505" or "uq_contact_lookup_actions_quote" not in (
            fields.get("constraint_name") or ""
        ):
            raise
        await db.rollback()
        winner = await _action_for_quote(db, body.quote_id, user_id)
        if winner is None:
            raise
        return _replayed(winner, job_id, body.category)

    # Committed: the purchase is durable. Nothing below may fail the request.
    try:
        await _bounded(r.eval, _DELETE_IF_SAME_QUOTE, 1, key, body.quote_id)
    except Exception:  # noqa: BLE001 - the key expires; a replay is answered from the DB
        _logger.warning("contact lookup %s: quote key not cleared", action_id)

    try:
        from src.workers.contact_lookup_action import lookup_contacts

        published = await _publish_contact_lookup(lookup_contacts, action_id)
    except Exception:  # noqa: BLE001 - committed; the reconciler publishes it
        _logger.warning("contact lookup %s: publish not attempted", action_id, exc_info=True)
        published = False
    if published:
        try:
            # Zero rows is success: the worker already started it (AO2). The guard
            # allows exactly this stamp, once, while the action is dispatching.
            await db.execute(
                text("UPDATE contact_lookup_actions SET dispatched_at = now() "
                     "WHERE id = CAST(:a AS uuid) AND user_id = CAST(:u AS uuid) "
                     "  AND status = 'dispatching' AND dispatched_at IS NULL"),
                params,
            )
            await db.commit()
        except Exception:  # noqa: BLE001 - only the publish clock; P3 tolerates NULL
            await db.rollback()
            _logger.warning("contact lookup %s: dispatched_at not stamped", action_id,
                            exc_info=True)

    # `user_id`, never `current_user.id`: a rollback above (a failed stamp) expires the
    # ORM user, and reading it would lazy-load on an async session (MissingGreenlet),
    # turning a committed purchase into a 500.
    audit_log(request, "contact_lookup_confirmed", user_id,
              f"action_id={action_id} quoted_count={len(ids)}")
    return ContactLookupAction(action_id=action_id, status="dispatching",
                               quoted_count=len(ids), truncated=truncated)


@router.get("/{job_id}/logs")
async def stream_logs(
    job_id: str,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> StreamingResponse:
    """SSE endpoint: replays existing logs then streams new ones via Redis Pub/Sub.

    A finished job replays and ends with ``{"type": "done"}``. A running job
    needs a stream lease (``src/api/sse_leases.py``); over the per-user cap the
    request is refused with 429 + Retry-After before any stream opens. A live
    stream ends with the job's terminal event, or ``{"type": "timeout"}`` after
    30 minutes or if its lease was reclaimed, after which the client reconnects.
    Nothing here affects the job itself: the worker never reads stream state.
    """
    # Verify ownership
    result = await db.execute(
        select(Job).where(Job.id == job_id, Job.user_id == current_user.id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    user_id = str(current_user.id)

    if job.status in _SSE_TERMINAL_STATUSES:
        # No lease bounds a replay, so the request budget does (audit #3 S3-09):
        # each call reads and ships every stored log line.
        await rate_limit(request, zone="general", identifier=current_user.id)
        # A finished job streams nothing live, so it takes no slot. The query
        # filters on user_id itself (C7, see _job_logs_select), so swapping
        # get_rls_db for get_db here would not read across tenants.
        replay = [_sse_log_line(log) for log in (await db.execute(_job_logs_select(job_id, user_id))).scalars()]
        await db.commit()

        async def replay_only() -> AsyncGenerator[str, None]:
            for line in replay:
                yield line
            yield "data: {\"type\": \"done\"}\n\n"

        return StreamingResponse(replay_only(), media_type="text/event-stream", headers=_SSE_HEADERS)

    # End the read transaction before streaming. The async engine is NullPool,
    # so an open transaction pins a real Postgres connection "idle in
    # transaction" for the life of the stream, up to 30 minutes (measured
    # 2026-09-13). The stream does its own reads on short-lived sessions.
    await db.commit()

    admission = await sse_leases.acquire(user_id)
    if not admission.admitted:
        _logger.info(
            "sse rejected user=%s job=%s active=%d cap=%d retry_after=%ds",
            user_id, job_id, admission.active, settings.SSE_MAX_STREAMS_PER_USER,
            admission.retry_after_seconds,
        )
        # A 429 before any stream opens, not a 200 carrying an error "log line":
        # the client can tell "live view unavailable" from job output. The job
        # itself is unaffected; only this view of it was refused.
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Live log stream limit reached for this account. The job is still running.",
            headers={"Retry-After": str(admission.retry_after_seconds)},
        )
    lease_id = admission.lease_id
    _logger.info("sse opened user=%s job=%s active=%d", user_id, job_id, admission.active)

    async def event_stream() -> AsyncGenerator[str, None]:
        opened = time.monotonic()
        end_reason = "client_disconnect"
        pubsub = None
        try:
            pubsub = sse_leases.get_redis().pubsub()
            # Subscribe BEFORE reading stored lines. The worker commits a line
            # before publishing it (_publish_log), so every line is either in
            # this read or published after the subscription. A line in both
            # carries the same id and is dropped by the client.
            await pubsub.subscribe(f"job_logs:{job_id}")
            for line in await _stream_stored_log_lines(job_id, user_id):
                yield line

            next_renew = opened + sse_leases.LEASE_HEARTBEAT_SECONDS
            next_status_check = opened  # first pass: catch a job that ended before we subscribed
            next_keepalive = opened + _SSE_KEEPALIVE_SECONDS
            while True:
                now = time.monotonic()
                if now - opened > _SSE_MAX_DURATION_SECONDS:
                    end_reason = "max_duration"
                    yield "data: {\"type\": \"timeout\"}\n\n"
                    break
                if now >= next_renew:
                    next_renew = now + sse_leases.LEASE_HEARTBEAT_SECONDS
                    if not await sse_leases.renew(user_id, lease_id):
                        # The slot was reclaimed (renewals stalled past the TTL).
                        # Holding the stream would exceed the cap; the client
                        # reconnects through admission instead.
                        end_reason = "lease_lost"
                        yield "data: {\"type\": \"timeout\"}\n\n"
                        break
                if now >= next_status_check:
                    # Cancellation and some recovery paths terminalize a job
                    # without publishing an event; without this the stream would
                    # hold its slot for the full 30 minutes.
                    next_status_check = now + _SSE_STATUS_CHECK_SECONDS
                    current = await _stream_job_status(job_id, user_id)
                    if current is None or current in _SSE_TERMINAL_STATUSES:
                        end_reason = "job_terminal"
                        if current is not None:
                            yield f"data: {{\"type\": \"{current}\"}}\n\n"
                        break

                if now >= next_keepalive:
                    next_keepalive = now + _SSE_KEEPALIVE_SECONDS
                    # A comment, not an event: no client sees a log line for it.
                    yield ": keepalive\n\n"

                # The keepalive deadline joins the others rather than replacing
                # them: lease renewal and the terminal-status check must keep
                # their own cadence, and the 30-minute expiry is still enforced
                # at the top of the loop, so a quiet stream cannot outlive it.
                wait = max(
                    0.0,
                    min(next_renew, next_status_check, next_keepalive) - time.monotonic(),
                )
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=wait)
                if message and message.get("type") == "message":
                    yield f"data: {message['data']}\n\n"
                    try:
                        data = json.loads(message["data"])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(data, dict) and data.get("type") in _SSE_TERMINAL_STATUSES:
                        end_reason = "job_terminal"
                        break
        except Exception:
            end_reason = "error"
            _logger.exception("sse stream error user=%s job=%s", user_id, job_id)
            raise
        finally:
            # Both are shielded and time-bounded: on client disconnect this
            # task is cancelled, and an unshielded await here is cancelled too
            # (that is how the old counter leaked). Neither raises, so the
            # release always runs; the lease expires even if it fails.
            if pubsub is not None:
                await sse_leases.close_pubsub(pubsub, job_id)
            await sse_leases.release(user_id, lease_id)
            _logger.info(
                "sse closed user=%s job=%s reason=%s lifetime=%.1fs",
                user_id, job_id, end_reason, time.monotonic() - opened,
            )

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)


def _job_logs_select(job_id: str, user_id: str):
    """A job's stored log lines, oldest first, tenant-filtered.

    C7 (full-SaaS review): JobLog has no user_id column, so the query joins
    through Job with an explicit user_id filter. The caller's ownership check
    already proves the job is theirs; this keeps the tenant boundary at the
    ORM layer even if a caller ever runs it on a session without RLS.
    """
    return (
        select(JobLog)
        .join(Job, JobLog.job_id == Job.id)
        .where(JobLog.job_id == job_id, Job.user_id == user_id)
        .order_by(JobLog.created_at.asc())
    )


def _sse_log_line(log: JobLog) -> str:
    return f"data: {LogLine.model_validate(log).model_dump_json()}\n\n"


def _stream_session(user_id: str) -> AsyncSession:
    """A short-lived RLS-bound session for work inside a live stream.

    The request's own session is committed before streaming begins, and
    reusing it would reopen a transaction held for the stream's lifetime.
    The binding is the one get_rls_db applies, re-applied per transaction by
    the after_begin listener; queries still filter on user_id explicitly.
    """
    session = db_session.AsyncSessionLocal()
    session.sync_session.info["rls_user_id"] = user_id
    return session


async def _stream_job_status(job_id: str, user_id: str) -> str | None:
    """Current status of the streamed job."""
    async with _stream_session(user_id) as session:
        result = await session.execute(
            select(Job.status).where(Job.id == job_id, Job.user_id == user_id)
        )
        return result.scalar_one_or_none()


async def _stream_stored_log_lines(job_id: str, user_id: str) -> list[str]:
    """Every stored log line of the streamed job, as SSE events."""
    async with _stream_session(user_id) as session:
        return [_sse_log_line(log) for log in (await session.execute(_job_logs_select(job_id, user_id))).scalars()]


# ─── Export URL (presigned R2 download) ──────────────────────────────────────

@router.get("/{job_id}/export-url", tags=["jobs"])
async def get_export_url(
    job_id: str,
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
    # Phase 4: carry the tax view-filters through so the in-app export flow
    # (export-url -> download) produces a CSV that matches the filtered view,
    # rather than silently downloading the unfiltered set (Codex).
    min_amount: float | None = Query(None, ge=0, le=100_000_000),
    max_amount: float | None = Query(None, ge=0, le=100_000_000),
    # Bounded (Codex security): an unbounded months value produces an
    # out-of-int4 bill_year comparison bound -> Postgres "integer out of range"
    # error / log churn. 1200 months = 100y, safely above any real delinquency.
    min_months: int | None = Query(None, ge=0, le=1200),
    max_months: int | None = Query(None, ge=0, le=1200),
    dialer_ready: bool = Query(False),  # Phase 5: carry dialer filter through too
    absentee: bool | None = Query(None),       # Tier 0 (057): owner-location filters
    out_of_state: bool | None = Query(None),
    # Which Results view the file is for (see results_category). Carried through
    # like the view filters: the token stays bound to this user and this job.
    category: ResultsCategory = Query(DEFAULT_RESULTS_CATEGORY),
) -> dict:
    """Return a short-lived download URL for the job's CSV export.

    Generates a single-use token (60s) scoped to this job + user.
    The token is safe to put in a URL — it's not the full JWT.
    """
    await rate_limit(request, zone="export", identifier=user.id)  # audit #3 S3-09
    result = await db.execute(
        select(Job).where(Job.id == job_id, Job.user_id == user.id)
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not _run_delivered(job):
        raise _undelivered_run_409()
    if not job.export_key:
        raise HTTPException(status_code=404, detail="No export available yet")

    # Generate a short-lived download token (60 seconds, scoped to
    # this job). H6 (full-SaaS review): include aud/iss/jti claims
    # alongside the existing sub/job_id/purpose/exp so (a) tokens
    # minted for a different purpose cannot be reused as downloads,
    # (b) the token can be distinguished from full session JWTs
    # during verification, and (c) a jti lets us blacklist a
    # download link in the rare case we need to revoke one before
    # its 60s TTL expires.
    # Shared mint helper (also used by worker delivery). Claims: sub/job_id/
    # purpose/aud/iss/jti/iat/exp. iat lets the logout-all revocation check
    # compare the token's age against the user's most recent /auth/logout-all.
    from src.api.download_tokens import mint_download_token
    download_token = mint_download_token(str(user.id), job_id, ttl_seconds=60)

    # Append any active tax filters so the download matches the filtered view.
    from urllib.parse import urlencode
    query: dict = {"token": download_token}
    for key, val in (
        ("min_amount", min_amount),
        ("max_amount", max_amount),
        ("min_months", min_months),
        ("max_months", max_months),
    ):
        if val is not None:
            query[key] = val
    if dialer_ready:
        query["dialer_ready"] = "true"
    # Owner-location filters carry through too (lowercase bools for the query string).
    if absentee is not None:
        query["absentee"] = "true" if absentee else "false"
    if out_of_state is not None:
        query["out_of_state"] = "true" if out_of_state else "false"
    if category != DEFAULT_RESULTS_CATEGORY:
        query["category"] = category
    return {"url": f"/jobs/{job_id}/download?{urlencode(query)}"}


async def _user_from_download_token(token: str, job_id: str, db: AsyncSession) -> User:
    """Resolve the owner of a ``purpose=download`` token, or refuse.

    Strict decode against the download audience only, so a session JWT (audience
    ``bridgeleads-api``) never authenticates through the query string. Honors
    both revocation paths a download token can have: its own jti, and the
    owner's logout-all. Redis being unreachable is a 503, never a pass: without
    it the token cannot be proven unrevoked.
    """
    import jwt as jose_jwt
    from jwt.exceptions import InvalidTokenError as JWTError

    from src.api.middleware.auth_hardening import TokenBlacklist, revocation_unavailable_503

    try:
        payload = jose_jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=["HS256"],
            audience="bridgeleads-download",
            issuer="bridgeleads",
            options={"require": ["exp", "aud", "iss"]},
        )
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired download link")

    user_id = payload.get("sub")
    if payload.get("purpose") != "download" or not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired download link")
    if payload.get("job_id") != job_id:
        raise HTTPException(status_code=403, detail="Token not valid for this job")

    # A token minted before the iat claim existed: its lifetime is fixed at
    # issue, so exp - 60 is its issue time (the 60 s /export-url token; the 48 h
    # emailed links always carry iat).
    issued_at = payload.get("iat")
    if issued_at is None:
        issued_at = max(0, int(payload["exp"]) - 60)

    try:
        jti = payload.get("jti", "")
        if jti and await TokenBlacklist.is_blacklisted(jti):
            raise HTTPException(status_code=401, detail="Token revoked")
        if await TokenBlacklist.is_revoked_by_user_logout_all(user_id, issued_at):
            raise HTTPException(status_code=401, detail="Token revoked")
    except _redis_exceptions.RedisError:
        raise revocation_unavailable_503()

    # is_active (audit 2026-09-25, D-1): an emailed link lives 48h, and a
    # deactivated account must not keep downloading through one.
    user = (
        await db.execute(select(User).where(User.id == user_id, User.is_active))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user


@router.get("/{job_id}/download", tags=["jobs"])
async def download_export(
    job_id: str,
    token: str = Query(default=""),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    # Phase 4: same tax view-filters as get_results so the export matches the
    # filtered view exactly. Optional; absent = full export (unchanged behavior).
    min_amount: float | None = Query(None, ge=0, le=100_000_000),
    max_amount: float | None = Query(None, ge=0, le=100_000_000),
    # Bounded (Codex security): an unbounded months value produces an
    # out-of-int4 bill_year comparison bound -> Postgres "integer out of range"
    # error / log churn. 1200 months = 100y, safely above any real delinquency.
    min_months: int | None = Query(None, ge=0, le=1200),
    max_months: int | None = Query(None, ge=0, le=1200),
    dialer_ready: bool = Query(False),
    absentee: bool | None = Query(None),       # Tier 0 (057): owner-location filters
    out_of_state: bool | None = Query(None),
    # new (default, the delivered file) or already_delivered (the rows an earlier
    # run of this account already delivered, for the Results view of the same name).
    category: ResultsCategory = Query(DEFAULT_RESULTS_CATEGORY),
):
    """Build and stream the lead CSV LIVE from the DB (not from R2).

    Uses the shared canonical builder (src/utils/lead_export.py), so this download
    is byte-for-byte the same dialer-ready format as the scheduled/emailed export.
    Reading live means skip-trace phone/email appear as soon as the dispatcher
    completes, even if the original scheduled export ran before skip-trace.

    Accepts a short-lived download token (from /export-url) OR an Authorization header.
    The download token is scoped to a specific job, expires in 60s, and is safe for URLs.
    """
    # Two ways in (audit #3, S3-07). ?token= carries ONLY a job-bound download token
    # (purpose=download, audience bridgeleads-download), minted by /export-url (60 s)
    # or by the worker for emailed links; a session JWT there is refused, since bearer
    # credentials do not belong in URLs, history or access logs. The Authorization
    # header goes through get_auth_context, the single decode point, so every
    # revocation it enforces (token blacklist, logout-all, the session family) applies
    # here too. This route used to re-implement the check, missed the session family,
    # and kept serving signed-out sessions.
    if token:
        user = await _user_from_download_token(token, job_id, db)
    else:
        header = request.headers.get("authorization", "") if request else ""
        scheme, _, credentials = header.partition(" ")
        if scheme.lower() != "bearer" or not credentials.strip():
            raise HTTPException(status_code=401, detail="Authentication required")
        ctx = await get_auth_context(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=credentials.strip()), db
        )
        user = ctx.user

    await rate_limit(request, zone="export", identifier=user.id)  # audit #3 S3-09

    # Set RLS context BEFORE any tenant read so the Job/Result queries get the
    # RLS belt in addition to the explicit user_id filter. This route uses
    # get_db (not get_rls_db) because the user is resolved from a download
    # token rather than the standard dependency, so we set the context here.
    # Also store it on session.info so the after_begin listener (session.py)
    # re-applies the GUC if this path ever commits mid-request — consistent
    # with get_rls_db and forward-safe for the non-BYPASSRLS cutover role.
    db.sync_session.info["rls_user_id"] = str(user.id)
    await db.execute(
        text("SELECT set_config('app.current_user_id', :uid, true)"),
        {"uid": str(user.id)},
    )

    result = await db.execute(
        select(Job).where(Job.id == job_id, Job.user_id == user.id)
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not _run_delivered(job):
        raise _undelivered_run_409()
    if not job.export_key:
        raise HTTPException(status_code=404, detail="No export available yet")

    import io

    try:
        # RLS context already set above (before the Job ownership read).
        # Generate CSV directly from database results
        from datetime import UTC, datetime
        today = datetime.now(UTC).date()
        dl_query = select(Result).where(Result.job_id == job_id, Result.user_id == user.id)
        # Phase 4: apply the SAME tax view-filters as get_results so the export
        # matches the filtered view. Track whether a filter is active so an
        # empty filtered set returns a header-only CSV (a valid "no matches")
        # rather than the 404 used for a genuinely empty job.
        tax_conditions = build_tax_conditions(
            min_amount, max_amount, min_months, max_months, today
        )
        for cond in tax_conditions:
            dl_query = dl_query.where(cond)
        # Hard product cap: never EXPORT tax rows whose oldest unpaid year is >18
        # months old, regardless of user filters. Matches get_results.
        dl_query = dl_query.where(tax_cap_condition(today))
        # Standing rules (match get_results + the worker exports): unactionable rows
        # and duplicates are never exported. None of these three is a user "filter" —
        # they are product rules, which is why the empty-result branch below probes
        # for rows using only the quarantine rules and asks nothing about them.
        dl_query = dl_query.where(actionable_condition())
        # Default: new leads only, exactly as before. The already_delivered file is
        # the same set that view lists; a download bills nothing either way.
        dl_query = dl_query.where(category_condition(category))
        # Phase 5: dialer-ready filter (not known-DNC; matches get_results +
        # the push — strict IS-FALSE would hide skip-traced phones whose DNC is
        # NULL; the dialer scrubs DNC).
        if dialer_ready:
            for cond in dialer_ready_conditions(include_unknown_dnc=True):
                dl_query = dl_query.where(cond)
        # Tier 0 (057): owner-location filters (match get_results).
        owner_conditions = build_owner_conditions(absentee, out_of_state)
        for cond in owner_conditions:
            dl_query = dl_query.where(cond)

        # Deterministic order (groups an estate's records together) — the SAME
        # order the scheduled/R2 export uses, so the two exports are byte-identical,
        # not just same-columns (Codex).
        dl_query = dl_query.order_by(
            Result.party_name, Result.date_recorded, Result.id
        )

        results_query = await db.execute(dl_query)
        records = results_query.scalars().all()

        if not records:
            # A genuinely empty job still 404s (existing contract). Everything else
            # gets a valid header-only CSV.
            #
            # Two ways to arrive here with rows in the DB:
            #  1. a USER filter matched nothing — "no matches", not an empty job;
            #  2. every deliverable row is a DUPLICATE. That is the whole shape of an
            #     all-duplicate run, and the completion email for such a job still
            #     links here — so 404ing it would hand the user a dead download for a
            #     job the product legitimately reports as "0 records" (Codex).
            # The probe therefore runs unconditionally and asks "did this job persist
            # any actionable, in-cap row at all", INDEPENDENT of the duplicate rule.
            exists_row = await db.execute(
                select(Result.id)
                .where(
                    Result.job_id == job_id,
                    Result.user_id == user.id,
                    # "Has rows" means the job persisted a row with a usable
                    # ADDRESS. Deliberately the address half only: every other rule
                    # here (duplicate, over-plan-quota, tax cap) says a row is not
                    # DELIVERABLE, which is precisely the header-only case — the job
                    # produced rows, none of them ship, and its completion email
                    # still links here. Using the full actionable_condition() would
                    # 404 an all-over-quota job the same way it used to 404 an
                    # all-duplicate one (Codex). Only a job with no addressable row
                    # at all is genuinely empty.
                    has_address_condition(),
                )
                .limit(1)
            )
            job_has_any = exists_row.scalar_one_or_none() is not None
            if not job_has_any:
                raise HTTPException(status_code=404, detail="No records found for this job")

        # Build CSV in memory — includes skip trace fields (phone, email)
        # when available. The download always reads LIVE from the DB, so
        # phone/email appear as soon as the skip trace dispatcher completes,
        # even if the original export was uploaded before skip trace ran.
        output = io.StringIO()
        # Canonical dialer-ready CSV via the shared builder — the SAME format the
        # scheduled/R2 export uses, so every export path produces an identical file
        # (no "use the in-app download for dialers" caveat). This reads LIVE DB rows,
        # so skip-trace phone/email appear as soon as the dispatcher completes.
        # Honor the user's output-field visibility for this job's config (blank
        # deselected hideable columns; identity/derived columns always present).
        # Loaded scoped to the owner (RLS belt + explicit user filter); legacy/empty
        # fields => show everything. Covers batch children too: each child is its OWN
        # ScraperConfig carrying the batch's `fields` (batches.py), and Job.scraper_
        # config_id is NOT NULL, so the guard's None branch is defensive only. (The
        # batch COMBINED export is a separate path — see batch_export.py.)
        from src.utils.lead_export import (
            resolve_export_layout,
            resolve_hidden_output_fields,
            write_lead_csv,
        )
        hidden_fields: set[str] = set()
        # Lean per-record-type columns: this download is a SINGLE record type (each
        # job — batch child or standalone — has one ScraperConfig.record_type). The
        # combined batch export is a separate superset path (batch_export.py). None
        # scraper_config_id (defensive; Job.scraper_config_id is NOT NULL) -> full.
        columns: list[str] | None = None
        labels: dict[str, str] | None = None
        # Source county/state/record_type for the rows: a Result carries none of
        # them, and without a record type the party-name order is unknown (blank
        # First/Last). Read from the SAME owner-scoped config row as the layout.
        context: dict[str, str] | None = None
        if job.scraper_config_id:
            cfg_row = await db.execute(
                select(
                    ScraperConfig.fields, ScraperConfig.record_type, ScraperConfig.deliver,
                    ScraperConfig.county, ScraperConfig.state,
                ).where(
                    ScraperConfig.id == job.scraper_config_id,
                    ScraperConfig.user_id == user.id,
                )
            )
            cfg = cfg_row.one_or_none()
            if cfg is not None:
                hidden_fields = resolve_hidden_output_fields(cfg.fields)
                layout = cfg.deliver.get("csv_layout") if isinstance(cfg.deliver, dict) else None
                columns, labels = resolve_export_layout(layout, cfg.record_type)
                context = {
                    "county": cfg.county, "state": cfg.state, "record_type": cfg.record_type,
                }
        write_lead_csv(
            records, output, hidden_fields=hidden_fields, columns=columns,
            labels=labels, context=context,
        )

        csv_bytes = output.getvalue().encode("utf-8")
        # The two files must not be confused once they sit in a downloads folder.
        filename = (
            f"bridgeleads_{job_id[:8]}_already_delivered.csv"
            if category == "already_delivered"
            else f"bridgeleads_{job_id[:8]}.csv"
        )

        from starlette.background import BackgroundTask
        from starlette.responses import Response

        from src.api.download_tracking import mark_leads_downloaded

        return Response(
            content=csv_bytes,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                # no-store: the file is built LIVE (skip-trace phones, the scraper's
                # CSV layout). A cached copy served a stale file for an hour, e.g. the
                # old headers after a layout switch (local browser check, 2026-09-15),
                # and owner PII should not sit in a shared browser cache anyway.
                "Cache-Control": "no-store",
            },
            # Activation signal, recorded AFTER the bytes go out. As a background
            # task it cannot turn a bookkeeping failure into a failed download,
            # and it cannot be reached by the `except Exception -> 500` below.
            # Only when the file carried leads: the header-only responses above
            # (all-duplicate, all-over-quota, a filter that matched nothing) are
            # a valid CSV but not leads in anyone's hands.
            background=(
                BackgroundTask(mark_leads_downloaded, str(user.id))
                if records else None
            ),
        )
    except HTTPException:
        raise
    except Exception:
        _logger.exception("Download error for job %s", job_id)
        raise HTTPException(status_code=500, detail="Download temporarily unavailable")
