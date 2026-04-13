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
from src.api.middleware import audit_log, rate_limit, sanitize_for_csv, sanitize_search
from src.api.schemas import JobCreate, JobResponse, LogLine, ResultRow, ResultsPage
from src.config import settings
from src.db import CountyConnector, Job, JobLog, Result, ScraperConfig, User
from src.utils.logger import setup_logger

_logger = setup_logger("api.jobs")

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
) -> dict:
    """Return a short-lived download URL for the job's CSV export.

    Generates a single-use token (60s) scoped to this job + user.
    The token is safe to put in a URL — it's not the full JWT.
    """
    import jwt as jose_jwt
    from src.config import settings as app_settings
    import time

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
    import uuid as _uuid
    download_token = jose_jwt.encode(
        {
            "sub": str(user.id),
            "job_id": job_id,
            "purpose": "download",
            "aud": "bridgeleads-download",
            "iss": "bridgeleads",
            "jti": _uuid.uuid4().hex,
            "exp": int(time.time()) + 60,  # 60 second expiry
        },
        app_settings.SECRET_KEY,
        algorithm="HS256",
    )

    return {"url": f"/jobs/{job_id}/download?token={download_token}"}


@router.get("/{job_id}/download", tags=["jobs"])
async def download_export(
    job_id: str,
    token: str = Query(default=""),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    """Stream the CSV export directly from R2.

    Accepts a short-lived download token (from /export-url) OR an Authorization header.
    The download token is scoped to a specific job, expires in 60s, and is safe for URLs.
    """
    import requests as sync_requests
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
        # Decode WITHOUT aud verification first so we can branch on
        # purpose + apply the right aud check. pyjwt does not support
        # "try multiple audiences" natively; the manual check below
        # enforces aud after we know which kind of token this is.
        payload = jose_jwt.decode(
            auth_token,
            app_settings.SECRET_KEY,
            algorithms=["HS256"],
            options={"verify_exp": True, "verify_aud": False},
        )
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")

        from src.api.middleware.auth_hardening import TokenBlacklist

        if payload.get("purpose") == "download":
            # H6 (full-SaaS review): download tokens must carry the
            # bridgeleads-download audience + bridgeleads issuer so
            # they cannot be confused with full session JWTs, and
            # the jti must not appear in the blacklist (lets us
            # revoke a specific download link within its 60s TTL
            # if needed). Legacy tokens minted before H6 landed have
            # no aud/iss — we accept them during a grace period
            # bounded by their natural 60s expiry, after which no
            # legacy tokens can exist.
            legacy_no_aud = "aud" not in payload
            if not legacy_no_aud:
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
        else:
            # Full session JWT — check audience, issuer, blacklist
            if payload.get("aud") != "bridgeleads-api" or payload.get("iss") != "bridgeleads":
                raise HTTPException(status_code=401, detail="Invalid token claims")
            jti = payload.get("jti", "")
            if jti and await TokenBlacklist.is_blacklisted(jti):
                raise HTTPException(status_code=401, detail="Token revoked")

    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired download link")
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

        # Build CSV in memory — includes skip trace fields (phone, email)
        # when available. The download always reads LIVE from the DB, so
        # phone/email appear as soon as the skip trace dispatcher completes,
        # even if the original export was uploaded before skip trace ran.
        output = io.StringIO()
        fieldnames = [
            "date_recorded", "party_name", "heirs", "parcel_id",
            "property_address", "mailing_address", "legal_description",
            "phone", "phone_type", "email",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for r in records:
            writer.writerow({
                "date_recorded": sanitize_for_csv(r.date_recorded),
                "party_name": sanitize_for_csv(r.party_name),
                "heirs": sanitize_for_csv(r.heirs),
                "parcel_id": sanitize_for_csv(r.parcel_id),
                "property_address": sanitize_for_csv(r.property_address),
                "mailing_address": sanitize_for_csv(r.mailing_address),
                "legal_description": sanitize_for_csv(r.legal_description),
                "phone": sanitize_for_csv(getattr(r, "phone", None)),
                "phone_type": sanitize_for_csv(getattr(r, "phone_type", None)),
                "email": sanitize_for_csv(getattr(r, "email", None)),
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
