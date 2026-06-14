"""Job routes: CRUD + SSE live log stream."""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator

import redis.asyncio as aioredis
import redis.exceptions as _redis_exceptions
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import CurrentUser
from src.api.deps import get_db, get_rls_db
from src.api.dialer_filters import dialer_ready_conditions
from src.api.middleware import audit_log, rate_limit, sanitize_search
from src.api.owner_filters import build_owner_conditions
from src.api.schemas import JobCreate, JobResponse, LogLine, ResultRow, ResultsPage
from src.api.tax_filters import build_tax_conditions
from src.config import settings
from src.config.constants import CANCELLABLE_STATUSES, PRIORITY_QUEUE_PLANS
from src.db import CountyConnector, Job, JobLog, Result, ScraperConfig, User
from src.utils.logger import setup_logger

_logger = setup_logger("api.jobs")

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=list[JobResponse])
async def list_jobs(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> list[JobResponse]:
    result = await db.execute(
        select(Job)
        .where(Job.user_id == current_user.id)
        .order_by(Job.created_at.desc())
        .limit(100)
    )
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
        resp = JobResponse.model_validate(j)
        sc = config_map.get(str(j.scraper_config_id))
        if sc:
            resp.scraper_name = sc.name
            resp.county = sc.county
            resp.state = sc.state
            resp.record_type = sc.record_type
        responses.append(resp)
    return responses


@router.post("", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: JobCreate,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> JobResponse:
    await rate_limit(request, zone="jobs", identifier=current_user.id)

    # Verify scraper config belongs to user
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

    # Check if this is an AI-powered connector and enforce AI job limits
    connector_result = await db.execute(
        select(CountyConnector).where(
            CountyConnector.county == config.county,
            func.lower(CountyConnector.state) == config.state.lower(),
            CountyConnector.active,
        )
    )
    connector = connector_result.scalars().first()
    if connector and getattr(connector, "scraper_mode", "manual") == "ai":
        ai_limit = settings.AI_JOB_LIMITS.get(current_user.plan, 5)
        if ai_limit != -1:
            # Count AI jobs this month. H4 (full-SaaS review): the
            # old query joined Job → ScraperConfig → CountyConnector
            # on (county, state) with no uniqueness constraint on
            # the connector side. Counties like Pierce have MULTIPLE
            # connector rows (manual + AI for different record
            # types), so the join produced duplicate rows per job
            # and the count was inflated — users hit their AI job
            # cap early. Fixed by counting DISTINCT Job.id and
            # filtering ScraperConfig.user_id explicitly so the
            # planner keeps the query tenant-scoped.
            from datetime import UTC, datetime
            month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            ai_job_count_result = await db.execute(
                select(func.count(func.distinct(Job.id))).select_from(Job).join(
                    ScraperConfig,
                    (Job.scraper_config_id == ScraperConfig.id)
                    & (ScraperConfig.user_id == current_user.id),
                ).join(
                    CountyConnector,
                    (func.lower(ScraperConfig.county) == func.lower(CountyConnector.county))
                    & (func.lower(ScraperConfig.state) == func.lower(CountyConnector.state))
                    & (CountyConnector.scraper_mode == "ai"),
                ).where(
                    Job.user_id == current_user.id,
                    Job.created_at >= month_start,
                )
            )
            ai_jobs_used = ai_job_count_result.scalar_one()
            if ai_jobs_used >= ai_limit:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail=(
                        f"Monthly AI scrape limit reached ({ai_jobs_used}/{ai_limit}). "
                        "Upgrade your plan for more AI-powered scrapes."
                    ),
                )

    # Enforce record limit — HTTP 402 when over quota
    if (
        current_user.records_limit != -1
        and current_user.records_used >= current_user.records_limit
    ):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Monthly record limit reached ({current_user.records_used}/{current_user.records_limit}). "
                "Upgrade your plan to continue."
            ),
        )

    job = Job(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        scraper_config_id=body.scraper_config_id,
        status="pending",
        trigger=body.trigger,
    )
    db.add(job)
    await db.flush()

    # Enqueue Celery task — paid plans get priority queue
    from src.workers.tasks import run_scrape_job

    queue = "scrape-priority" if current_user.plan in PRIORITY_QUEUE_PLANS else "scrape"
    run_scrape_job.apply_async(args=[job.id], queue=queue)

    audit_log(request, "job_created", current_user.id, f"job_id={job.id}")
    return JobResponse.model_validate(job)


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
    return JobResponse.model_validate(job)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_job(
    job_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> None:
    result = await db.execute(
        select(Job).where(Job.id == job_id, Job.user_id == current_user.id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    if job.status not in CANCELLABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel a job in '{job.status}' status",
        )
    job.status = "cancelled"
    await db.flush()


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

    # Show ALL results (new + duplicates). New leads appear first,
    # duplicates appear after — grayed out in the frontend so users
    # can see what was scraped while focusing on fresh leads.
    base_query = select(Result).where(
        Result.job_id == job_id,
        Result.user_id == current_user.id,
    )
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
    from datetime import UTC, datetime
    tax_conditions = build_tax_conditions(
        min_amount, max_amount, min_months, max_months, datetime.now(UTC).date()
    )
    for cond in tax_conditions:
        base_query = base_query.where(cond)

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
        base_query.order_by(Result.is_duplicate.asc(), Result.created_at.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [ResultRow.model_validate(r) for r in rows_result.scalars().all()]

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

    enriching = parcel_count > 0 and not enrichment_task_finished

    # Total scraped (including duplicates) and duplicate count
    total_scraped_result = await db.execute(
        select(func.count()).where(
            Result.job_id == job_id,
            Result.user_id == current_user.id,
        )
    )
    total_scraped = total_scraped_result.scalar_one()

    dup_count_result = await db.execute(
        select(func.count()).where(
            Result.job_id == job_id,
            Result.user_id == current_user.id,
            Result.is_duplicate.is_(True),
        )
    )
    duplicate_count = dup_count_result.scalar_one()

    # When results are empty (all duplicates or no new leads), find
    # the most recent previous job for the same county/record_type
    # that has actual Result rows, so the user can navigate there.
    # Searches across ALL scraper configs for the same county+type,
    # not just the same config_id.
    previous_job_id = None
    # Skip the empty-scrape "previous job" suggestion when ANY view filter is
    # active: total==0 then means "no rows matched the filter", NOT "the job
    # scraped nothing", and the prior job wasn't checked against the same filter
    # so suggesting it would be misleading (Codex).
    if total == 0 and config and not tax_conditions and not dialer_ready:
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
                select(Job.id)
                .where(
                    Job.scraper_config_id.in_(sibling_ids),
                    Job.user_id == current_user.id,
                    Job.id != job_id,
                    Job.status == "done",
                    exists(
                        select(Result.id).where(
                            Result.job_id == Job.id,
                            Result.is_duplicate.is_(False),
                        )
                    ),
                )
                .order_by(Job.created_at.desc())
                .limit(1)
            )
            prev_row = prev_result.scalar_one_or_none()
            if prev_row:
                previous_job_id = prev_row

    # NTS Tier 1: does this JOB have any auction-matched lead? Job-wide (NOT
    # page/filter-scoped) so the auction columns don't flicker by page when matches
    # are sparse (Codex). LIMIT 1 over ix_results_job_auction_date — cheap.
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

    return ResultsPage(
        job_id=job_id, total=total, page=page, page_size=page_size,
        items=items, enriched_count=enriched_count, enriching=enriching,
        total_scraped=total_scraped, duplicate_count=duplicate_count,
        date_range_mode=date_range_mode,
        previous_job_id=previous_job_id,
        has_auction_data=has_auction_data,
    )


@router.get("/{job_id}/logs")
async def stream_logs(
    job_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> StreamingResponse:
    """SSE endpoint: replays existing logs then streams new ones via Redis Pub/Sub."""
    # Verify ownership
    result = await db.execute(
        select(Job).where(Job.id == job_id, Job.user_id == current_user.id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    # Fetch existing logs for replay. C7 (full-SaaS review):
    # JobLog has no direct user_id column, so we join through Job
    # explicitly with a user_id filter. The ownership check above
    # already proves the caller owns this job_id, but adding the
    # filter here is defense-in-depth: if anyone ever swaps
    # get_rls_db for get_db on this route, the JobLog RLS policy
    # (which joins through jobs.user_id) would be bypassed and
    # the query would silently read across tenants. This explicit
    # filter keeps the tenant boundary at the ORM layer.
    logs_result = await db.execute(
        select(JobLog)
        .join(Job, JobLog.job_id == Job.id)
        .where(
            JobLog.job_id == job_id,
            Job.user_id == current_user.id,
        )
        .order_by(JobLog.created_at.asc())
    )
    existing_logs = logs_result.scalars().all()

    _MAX_SSE_PER_USER = 5
    _SSE_HEARTBEAT_TTL = 60  # Each connection key expires after 60s if not refreshed

    async def event_stream() -> AsyncGenerator[str, None]:
        # Track concurrent SSE connections using per-connection keys with TTL.
        # Each connection registers a unique key that auto-expires in 60s.
        # The polling loop refreshes the TTL — if the connection dies (crash,
        # network loss, tab close), the key expires automatically.
        conn_id = f"{current_user.id}:{uuid.uuid4().hex[:8]}"
        conn_key = f"sse_conn:{conn_id}"
        counter_key = f"sse_count:{current_user.id}"

        r = aioredis.from_url(settings.REDIS_URL, **settings.redis_kwargs())

        # C6 (full-SaaS review): atomic INCR-then-check instead of
        # scan_iter+count+set. Previously a user who opened 20 tabs
        # simultaneously could all pass the `conn_count < 5` check
        # before any of them wrote a key, leading to 20 stuck SSE
        # streams per user. Now we INCR first and decrement in the
        # finally block; if the incremented value exceeds the cap we
        # bail immediately and undo our own increment.
        conn_count = await r.incr(counter_key)
        # Expire the counter key so a long-lived overflow doesn't
        # poison future connections. Refreshed on each INCR.
        await r.expire(counter_key, _SSE_HEARTBEAT_TTL * 2)

        if conn_count > _MAX_SSE_PER_USER:
            # Undo our own increment so we don't block future
            # legitimate connections.
            await r.decr(counter_key)
            await r.aclose()
            yield f"data: {{\"type\": \"error\", \"message\": \"Too many concurrent streams (max {_MAX_SSE_PER_USER}). Close other tabs and retry.\"}}\n\n"
            return

        # Register this specific connection for heartbeat tracking
        await r.set(conn_key, "1", ex=_SSE_HEARTBEAT_TTL)

        try:
            # 1. Replay persisted logs
            for log in existing_logs:
                payload = LogLine.model_validate(log).model_dump_json()
                yield f"data: {payload}\n\n"

            # 2. If job is already terminal, stop here
            if job.status in {"done", "failed", "cancelled"}:
                yield "data: {\"type\": \"done\"}\n\n"
                return

            # 3. Subscribe to Redis Pub/Sub channel for live events
            import time as _time
            max_duration = 1800  # 30 minutes max SSE connection
            start_time = _time.time()
            last_heartbeat = _time.time()

            pubsub = r.pubsub()
            channel = f"job_logs:{job_id}"
            await pubsub.subscribe(channel)

            try:
                while True:
                    if _time.time() - start_time > max_duration:
                        yield "data: {\"type\": \"timeout\"}\n\n"
                        break

                    # Refresh TTL every 30s to prove connection is alive
                    if _time.time() - last_heartbeat > 30:
                        await r.expire(conn_key, _SSE_HEARTBEAT_TTL)
                        last_heartbeat = _time.time()

                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=30.0)
                    if message and message.get("type") == "message":
                        yield f"data: {message['data']}\n\n"
                        # Check for terminal event
                        try:
                            data = json.loads(message["data"])
                            if data.get("type") in {"done", "failed", "cancelled"}:
                                break
                        except (json.JSONDecodeError, KeyError):
                            pass
                    await asyncio.sleep(0.1)
            finally:
                await pubsub.unsubscribe(channel)
        finally:
            # Clean up — delete our connection key immediately and
            # decrement the atomic counter. The counter may go
            # temporarily negative if keys were deleted out of
            # order; `max(0, ...)` defensive clamp at the top of
            # the handler ensures a negative doesn't prevent the
            # next connection from being accepted.
            try:
                await r.delete(conn_key)
                await r.decr(counter_key)
            finally:
                await r.aclose()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering for SSE
        },
    )


# ─── Export URL (presigned R2 download) ──────────────────────────────────────

@router.get("/{job_id}/export-url", tags=["jobs"])
async def get_export_url(
    job_id: str,
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
) -> dict:
    """Return a short-lived download URL for the job's CSV export.

    Generates a single-use token (60s) scoped to this job + user.
    The token is safe to put in a URL — it's not the full JWT.
    """
    result = await db.execute(
        select(Job).where(Job.id == job_id, Job.user_id == user.id)
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
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
    return {"url": f"/jobs/{job_id}/download?{urlencode(query)}"}


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
):
    """Build and stream the lead CSV LIVE from the DB (not from R2).

    Uses the shared canonical builder (src/utils/lead_export.py), so this download
    is byte-for-byte the same dialer-ready format as the scheduled/emailed export.
    Reading live means skip-trace phone/email appear as soon as the dispatcher
    completes, even if the original scheduled export ran before skip-trace.

    Accepts a short-lived download token (from /export-url) OR an Authorization header.
    The download token is scoped to a specific job, expires in 60s, and is safe for URLs.
    """
    import jwt as jose_jwt
    from jwt.exceptions import InvalidTokenError as JWTError

    from src.config import settings as app_settings

    # Authenticate: prefer short-lived download token, fall back to Authorization header
    auth_token = token
    if not auth_token and request:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            auth_token = auth_header[7:]

    if not auth_token:
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        # REDTEAM LOW T1: do not branch security decisions on a token
        # whose audience/issuer were never verified. pyjwt cannot "try
        # multiple audiences" in one call, so we first read the
        # *unverified* claims (signature NOT trusted) only to learn which
        # kind of token this is, then RE-DECODE with pyjwt natively
        # enforcing the exact audience + issuer for that kind. A token
        # that lies about its purpose to dodge the audience check fails
        # the strict re-decode below.
        unverified = jose_jwt.decode(
            auth_token,
            app_settings.SECRET_KEY,
            algorithms=["HS256"],
            options={"verify_exp": True, "verify_aud": False},
        )
        is_download_token = unverified.get("purpose") == "download"
        expected_aud = "bridgeleads-download" if is_download_token else "bridgeleads-api"
        # Strict decode: pyjwt enforces aud + iss (raises if absent/wrong).
        payload = jose_jwt.decode(
            auth_token,
            app_settings.SECRET_KEY,
            algorithms=["HS256"],
            audience=expected_aud,
            issuer="bridgeleads",
            options={
                "verify_exp": True,
                "verify_aud": True,
                "verify_iss": True,
                "require": ["exp", "aud", "iss"],
            },
        )
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")

        from src.api.middleware.auth_hardening import TokenBlacklist, revocation_unavailable_503

        # Both branches honor BOTH revocation paths:
        #   1. is_blacklisted(jti) — this specific token was revoked
        #   2. get_user_revoke_time(user_id) — user clicked "log out
        #      everywhere"; any token issued before that must be
        #      rejected, including download tokens. Without #2 a
        #      user could /logout-all and an attacker holding a
        #      pre-revocation download link would still be served.
        #
        # Reconstruct iat for older download tokens that were minted
        # before the iat claim was added. We know download tokens have
        # a fixed 60s lifetime (see download_token mint above), so
        # `exp - 60` is the exact issuance time. Without this bridge,
        # any in-flight download token at deploy time would default
        # iat=0 and be falsely rejected by the user-revoke check
        # whenever the user has done /logout-all in the last 8 days.
        # Bridge is harmless after the 60s post-deploy window since
        # all such legacy tokens have expired.
        issued_at = payload.get("iat")
        if issued_at is None:
            if payload.get("purpose") == "download" and payload.get("exp"):
                issued_at = max(0, int(payload["exp"]) - 60)
            else:
                issued_at = 0

        if payload.get("purpose") == "download":
            # H6 (full-SaaS review): download tokens must carry the
            # bridgeleads-download audience + bridgeleads issuer so
            # they cannot be confused with full session JWTs, and
            # the jti must not appear in the blacklist (lets us
            # revoke a specific download link within its 60s TTL
            # if needed). The pre-H6 grace path that accepted tokens
            # without aud/iss has been removed — those tokens had a
            # 60-second lifetime and H6 has been deployed for many
            # months, so no such tokens can exist anywhere.
            if (
                payload.get("aud") != "bridgeleads-download"
                or payload.get("iss") != "bridgeleads"
            ):
                raise HTTPException(
                    status_code=401, detail="Invalid download token claims"
                )
            if payload.get("job_id") != job_id:
                raise HTTPException(status_code=403, detail="Token not valid for this job")
            jti = payload.get("jti", "")
            if jti and await TokenBlacklist.is_blacklisted(jti):
                raise HTTPException(status_code=401, detail="Token revoked")
            if await TokenBlacklist.is_revoked_by_user_logout_all(user_id, issued_at):
                raise HTTPException(status_code=401, detail="Token revoked")
        else:
            # Full session JWT — check audience, issuer, blacklist
            if payload.get("aud") != "bridgeleads-api" or payload.get("iss") != "bridgeleads":
                raise HTTPException(status_code=401, detail="Invalid token claims")
            jti = payload.get("jti", "")
            if jti and await TokenBlacklist.is_blacklisted(jti):
                raise HTTPException(status_code=401, detail="Token revoked")
            if await TokenBlacklist.is_revoked_by_user_logout_all(user_id, issued_at):
                raise HTTPException(status_code=401, detail="Token revoked")

    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired download link")
    except _redis_exceptions.RedisError:
        # Either is_blacklisted call above hit Redis — surface 503 so
        # the client can retry instead of treating the failure as a
        # 401-revoked. Same security boundary as the main JWT decoder
        # in src/api/auth.py: failing open here would silently accept
        # revoked download tokens.
        raise revocation_unavailable_503()
    except HTTPException:
        raise

    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

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
    if not job.export_key:
        raise HTTPException(status_code=404, detail="No export available yet")

    import io

    try:
        # RLS context already set above (before the Job ownership read).
        # Generate CSV directly from database results
        from datetime import UTC, datetime
        dl_query = select(Result).where(Result.job_id == job_id, Result.user_id == user.id)
        # Phase 4: apply the SAME tax view-filters as get_results so the export
        # matches the filtered view. Track whether a filter is active so an
        # empty filtered set returns a header-only CSV (a valid "no matches")
        # rather than the 404 used for a genuinely empty job.
        tax_conditions = build_tax_conditions(
            min_amount, max_amount, min_months, max_months, datetime.now(UTC).date()
        )
        for cond in tax_conditions:
            dl_query = dl_query.where(cond)
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
        # Any active filter -> an empty result is "no matches", not an empty job.
        any_filter_active = bool(tax_conditions) or dialer_ready or bool(owner_conditions)

        # Deterministic order (groups an estate's records together) — the SAME
        # order the scheduled/R2 export uses, so the two exports are byte-identical,
        # not just same-columns (Codex).
        dl_query = dl_query.order_by(
            Result.party_name, Result.date_recorded, Result.id
        )

        results_query = await db.execute(dl_query)
        records = results_query.scalars().all()

        if not records:
            # A genuinely empty job still 404s (existing contract). With a filter
            # active the filtered query can't tell "no matches" from "empty job",
            # so check unfiltered existence: rows exist but none matched -> a
            # valid header-only CSV; no rows at all -> 404 (Codex).
            job_has_any = False
            if any_filter_active:
                exists_row = await db.execute(
                    select(Result.id)
                    .where(Result.job_id == job_id, Result.user_id == user.id)
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
        from src.utils.lead_export import write_lead_csv
        write_lead_csv(records, output)

        csv_bytes = output.getvalue().encode("utf-8")

        from starlette.responses import Response

        return Response(
            content=csv_bytes,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="bridgeleads_{job_id[:8]}.csv"',
                "Cache-Control": "private, max-age=3600",
            },
        )
    except HTTPException:
        raise
    except Exception:
        _logger.exception("Download error for job %s", job_id)
        raise HTTPException(status_code=500, detail="Download temporarily unavailable")
