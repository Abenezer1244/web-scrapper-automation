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
from src.api.deps import get_rls_db
from src.api.middleware import audit_log, rate_limit, sanitize_search
from src.api.schemas import JobCreate, JobResponse, LogLine, ResultRow, ResultsPage
from src.config import settings
from src.db import Job, JobLog, Result, ScraperConfig

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
    if config_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scraper not found")

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

    # Enqueue Celery task (imported here to avoid circular imports at startup)
    from src.workers.tasks import run_scrape_job
    run_scrape_job.delay(job.id)

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

    return ResultsPage(job_id=job_id, total=total, page=page, page_size=page_size, items=items)


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
        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        pubsub = r.pubsub()
        channel = f"job_logs:{job_id}"
        await pubsub.subscribe(channel)

        try:
            while True:
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
