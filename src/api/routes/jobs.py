"""Job routes: CRUD + SSE live log stream."""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import CurrentUser
from src.api.deps import get_db, get_rls_db
from src.api.middleware import audit_log, rate_limit, sanitize_search
from src.api.schemas import JobCreate, JobResponse, LogLine, ResultRow, ResultsPage
from src.config import settings
from src.db import CountyConnector, Job, JobLog, Result, ScraperConfig, User

router = APIRouter(prefix="/jobs", tags=["jobs"])

_CANCELLABLE_STATUSES = {"pending", "queued", "scraping", "enriching"}


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
    return [JobResponse.model_validate(j) for j in result.scalars().all()]


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
    connector = connector_result.scalar_one_or_none()
    if connector and getattr(connector, "scraper_mode", "manual") == "ai":
        ai_limit = settings.AI_JOB_LIMITS.get(current_user.plan, 5)
        if ai_limit != -1:
            # Count AI jobs this month
            from datetime import UTC, datetime
            month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            ai_job_count_result = await db.execute(
                select(func.count()).select_from(Job).join(
                    ScraperConfig, Job.scraper_config_id == ScraperConfig.id
                ).join(
                    CountyConnector,
                    (func.lower(ScraperConfig.county) == func.lower(CountyConnector.county))
                    & (func.lower(ScraperConfig.state) == func.lower(CountyConnector.state)),
                ).where(
                    Job.user_id == current_user.id,
                    Job.created_at >= month_start,
                    CountyConnector.scraper_mode == "ai",
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

    priority_plans = {"business", "agency"}
    queue = "scrape-priority" if current_user.plan in priority_plans else "scrape"
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
    if job.status not in _CANCELLABLE_STATUSES:
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
    db: AsyncSession = Depends(get_rls_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    q: str | None = Query(None, max_length=100),
) -> ResultsPage:
    result = await db.execute(
        select(Job).where(Job.id == job_id, Job.user_id == current_user.id)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    safe_q = sanitize_search(q)

    base_query = select(Result).where(Result.job_id == job_id, Result.user_id == current_user.id)
    if safe_q:
        pattern = f"%{safe_q}%"
        base_query = base_query.where(
            Result.party_name.ilike(pattern, escape="\\")
            | Result.parcel_id.ilike(pattern, escape="\\")
            | Result.property_address.ilike(pattern, escape="\\")
        )

    count_result = await db.execute(
        select(func.count()).select_from(base_query.subquery())
    )
    total = count_result.scalar_one()

    rows_result = await db.execute(
        base_query.order_by(Result.created_at.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [ResultRow.model_validate(r) for r in rows_result.scalars().all()]

    # Count enriched records (have real property_address)
    enriched_result = await db.execute(
        select(func.count()).where(
            Result.job_id == job_id,
            Result.user_id == current_user.id,
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

    return ResultsPage(
        job_id=job_id, total=total, page=page, page_size=page_size,
        items=items, enriched_count=enriched_count, enriching=enriching,
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

    # Fetch existing logs for replay
    logs_result = await db.execute(
        select(JobLog).where(JobLog.job_id == job_id).order_by(JobLog.created_at.asc())
    )
    existing_logs = logs_result.scalars().all()

    async def event_stream() -> AsyncGenerator[str, None]:
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

        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        pubsub = r.pubsub()
        channel = f"job_logs:{job_id}"
        await pubsub.subscribe(channel)

        try:
            while True:
                if _time.time() - start_time > max_duration:
                    yield "data: {\"type\": \"timeout\"}\n\n"
                    break
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
) -> dict:
    """Return the direct download URL for the job's CSV export."""
    result = await db.execute(
        select(Job).where(Job.id == job_id, Job.user_id == user.id)
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.export_key:
        raise HTTPException(status_code=404, detail="No export available yet")

    # Return a URL to our own download proxy endpoint (avoids R2 auth issues)
    return {"url": f"/jobs/{job_id}/download"}


@router.get("/{job_id}/download", tags=["jobs"])
async def download_export(
    job_id: str,
    token: str = Query(default=""),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    """Stream the CSV export directly from R2 via Cloudflare API.

    Accepts auth via either Authorization header or ?token= query parameter
    (needed for window.open downloads where headers can't be set).
    """
    import requests as sync_requests
    from jose import jwt as jose_jwt, JWTError
    from src.config import settings as app_settings

    # Authenticate via query token or header
    auth_token = token
    if not auth_token and request:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            auth_token = auth_header[7:]

    if not auth_token:
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        payload = jose_jwt.decode(
            auth_token,
            app_settings.SECRET_KEY,
            algorithms=["HS256"],
            audience="bridgeleads-api",
            issuer="bridgeleads",
        )
        user_id = payload.get("sub")
        jti = payload.get("jti", "")
        iat = payload.get("iat", 0)
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token: no sub claim")

        # Check token blacklist (logout / revoke-all)
        from src.api.middleware.auth_hardening import TokenBlacklist
        if jti and await TokenBlacklist.is_blacklisted(jti):
            raise HTTPException(status_code=401, detail="Token has been revoked")
        revoke_time = await TokenBlacklist.get_user_revoke_time(user_id)
        if revoke_time and iat < revoke_time:
            raise HTTPException(status_code=401, detail="Token has been revoked")

    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired credentials")
    except HTTPException:
        raise

    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    result = await db.execute(
        select(Job).where(Job.id == job_id, Job.user_id == user.id)
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.export_key:
        raise HTTPException(status_code=404, detail="No export available yet")

    import csv
    import io
    from sqlalchemy import text

    try:
        # Set RLS context for this session (parameterized to prevent injection)
        await db.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": str(user.id)},
        )

        # Generate CSV directly from database results
        results_query = await db.execute(
            select(Result).where(Result.job_id == job_id, Result.user_id == user.id)
        )
        records = results_query.scalars().all()

        if not records:
            raise HTTPException(status_code=404, detail="No records found for this job")

        # Build CSV in memory
        output = io.StringIO()
        fieldnames = [
            "date_recorded", "party_name", "heirs", "parcel_id",
            "property_address", "mailing_address", "legal_description",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for r in records:
            writer.writerow({
                "date_recorded": r.date_recorded or "",
                "party_name": r.party_name or "",
                "heirs": r.heirs or "",
                "parcel_id": r.parcel_id or "",
                "property_address": r.property_address or "",
                "mailing_address": r.mailing_address or "",
                "legal_description": r.legal_description or "",
            })

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
    except Exception as exc:
        _logger.exception("Download error for job %s", job_id)
        raise HTTPException(status_code=500, detail="Download temporarily unavailable")
