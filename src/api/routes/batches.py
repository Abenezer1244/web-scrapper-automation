"""Batch scrape routes (Piece 2): create a multi-county x multi-record-type batch.

A batch fans out into N ordinary scrapes (one child ScraperConfig per county x
record_type) under a parent ScraperBatch holding the shared
fields/enrichment/deliver. The batch owns delivery (one combined CSV) — child
delivery + schedule are SUPPRESSED. The BatchRun + child Jobs are created async
by the dispatch worker (system-written), so this route only persists the parent
+ children, then kicks off the fan-out.
"""
import io
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import CurrentUser
from src.api.deps import get_rls_db
from src.api.middleware.rate_limit import rate_limit
from src.api.schemas import (
    BatchChildSummary,
    BatchCreateRequest,
    BatchCreateResponse,
    BatchDetailResponse,
    BatchSummaryResponse,
)
from src.config.constants import (
    BATCH_HARD_CEILING,
    BATCH_MAX_COMBINATIONS,
    BATCH_PLANS,
    BUSINESS_FEATURES_PLANS,
    SKIP_TRACE_ADDON_PLANS,
)
from src.db import CountyConnector
from src.db.models import BatchRun, Job, ScraperBatch, ScraperConfig
from src.utils.logger import setup_logger

_logger = setup_logger("api.batches")

router = APIRouter(prefix="/batches", tags=["batches"])


@router.post("", response_model=BatchCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_batch(
    body: BatchCreateRequest,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> BatchCreateResponse:
    await rate_limit(request, zone="general", identifier=current_user.id)
    plan = (current_user.plan or "starter").lower()

    # 1. Plan gate — batch is Pro+ (fans out into many paid scrapes).
    if plan not in BATCH_PLANS:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Batch scrape requires a Pro plan or higher.",
        )

    # 2. Size cap (cost/DoS blast radius) — per-plan, with a hard ceiling.
    combos = len(body.counties) * len(body.record_types)
    cap = min(BATCH_MAX_COMBINATIONS.get(plan, 0), BATCH_HARD_CEILING)
    if combos > cap:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"This batch is {combos} scrapes; your plan allows up to {cap} per batch.",
        )

    # 3. Delivery / skip-trace entitlement — SAME gates as a single scrape (a batch
    #    must not let a lower plan exfiltrate PII via webhook or run paid skip trace).
    if (body.deliver.webhook_url or body.deliver.dialer_webhook_url) and plan not in BUSINESS_FEATURES_PLANS:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Webhook delivery requires a Business or Agency plan",
        )
    if body.enrichment.skip_tracing and plan not in BUSINESS_FEATURES_PLANS:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Skip tracing enrichment requires a Business or Agency plan",
        )
    if body.skip_trace_enabled and plan not in SKIP_TRACE_ADDON_PLANS:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Skip trace requires a Pro plan or higher.",
        )

    # 4. Quota preflight — don't launch a batch when already at the monthly cap.
    #    (Records-per-scrape isn't predictable; this honest check blocks a batch
    #    that would only error at the quota wall.) -1/None = unlimited.
    limit = current_user.records_limit
    if limit is not None and limit >= 0 and current_user.records_used >= limit:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Monthly record limit reached — upgrade or wait for reset before running a batch.",
        )

    # 5. Validate EVERY (county, record_type) against the connector registry for
    #    this state. Reject the whole batch on any unsupported combo (predictable).
    rows = (
        await db.execute(
            select(CountyConnector).where(
                func.upper(CountyConnector.state) == body.state.upper(),
                CountyConnector.active,
            )
        )
    ).scalars().all()
    supported: dict[str, set[str]] = {}
    for c in rows:
        supported.setdefault(c.county.lower(), set()).update(rt.lower() for rt in c.record_types)
    invalid: list[str] = []
    for county in body.counties:
        avail = supported.get(county)
        if not avail:
            invalid.append(f"{county} (no active connector)")
            continue
        invalid.extend(f"{county}/{rt}" for rt in body.record_types if rt not in avail)
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported county/record-type combinations: {', '.join(invalid[:10])}",
        )

    # 6. Persist the parent batch (shared config) + child configs (delivery +
    #    schedule SUPPRESSED — the batch owns them). BatchRun + jobs are created by
    #    the dispatch worker (system-written).
    batch = ScraperBatch(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        name=body.name,
        state=body.state.upper(),
        fields=body.fields.model_dump(),
        enrichment=body.enrichment.model_dump(),
        schedule={},  # reserved for Phase 2B (scheduled batch)
        deliver=body.deliver.model_dump(),
        status="active",
    )
    db.add(batch)
    await db.flush()
    for county in body.counties:
        for rt in body.record_types:
            db.add(
                ScraperConfig(
                    id=str(uuid.uuid4()),
                    user_id=current_user.id,
                    batch_id=batch.id,
                    name=f"{county.title()} {rt} (batch)",
                    county=county,
                    state=body.state.upper(),
                    record_type=rt,
                    fields=body.fields.model_dump(),
                    enrichment=body.enrichment.model_dump(),
                    schedule={},   # suppressed — batch owns scheduling
                    deliver={},    # suppressed — batch owns delivery
                    skip_trace_enabled=body.skip_trace_enabled,
                )
            )
    # Commit so the rows are visible before the dispatch worker reads them
    # (get_rls_db tolerates a mid-handler commit — its SET LOCAL is re-applied).
    await db.commit()
    # Lazy import (matches the codebase) — keep the Celery app out of the API
    # import graph.
    from src.workers.batch_tasks import dispatch_batch_run
    dispatch_batch_run.delay(batch.id)
    _logger.info("batch %s created for user %s: %d scrapes", batch.id, current_user.id, combos)
    return BatchCreateResponse(batch_id=batch.id, child_count=combos, status="pending")


# ─── Read + download (Phase 2A.4) ─────────────────────────────────────────────
#
# ScraperBatch / BatchRun are system-written and NOT RLS-granted (mirror the
# dialer outbox), so the explicit `user_id == current_user.id` filter on every
# query is the ONLY tenant boundary for those tables. We still depend on
# get_rls_db so the RLS belt stays active for the joined RLS tables
# (scraper_configs, jobs). A run is at-most-one per batch in on-demand 2A.


def _summary(batch: ScraperBatch, run: BatchRun | None, child_count: int) -> BatchSummaryResponse:
    return BatchSummaryResponse(
        id=batch.id,
        name=batch.name,
        state=batch.state,
        run_status=run.status if run else "pending",
        child_count=child_count,
        combined_export_ready=bool(run and run.combined_export_key),
        created_at=batch.created_at,
        completed_at=run.completed_at if run else None,
    )


async def _owned_batch(db: AsyncSession, batch_id: str, user_id: str) -> ScraperBatch:
    batch = (
        await db.execute(
            select(ScraperBatch).where(
                ScraperBatch.id == batch_id, ScraperBatch.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")
    return batch


async def _run_for(db: AsyncSession, batch_id: str, user_id: str) -> BatchRun | None:
    # Tie the run to an OWNED batch via the join (Codex P2): batch_runs is not
    # RLS-granted, so don't rely on BatchRun.user_id alone — require a
    # scraper_batches row with the same id AND user_id to exist.
    return (
        await db.execute(
            select(BatchRun)
            .join(ScraperBatch, ScraperBatch.id == BatchRun.batch_id)
            .where(
                BatchRun.batch_id == batch_id,
                BatchRun.user_id == user_id,
                ScraperBatch.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


@router.get("", response_model=list[BatchSummaryResponse])
async def list_batches(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> list[BatchSummaryResponse]:
    """List the current user's batches, newest first, with run status."""
    batches = (
        await db.execute(
            select(ScraperBatch)
            .where(ScraperBatch.user_id == current_user.id)
            .order_by(ScraperBatch.created_at.desc())
        )
    ).scalars().all()
    if not batches:
        return []
    batch_ids = [b.id for b in batches]
    runs = (
        await db.execute(
            select(BatchRun).where(
                BatchRun.user_id == current_user.id, BatchRun.batch_id.in_(batch_ids)
            )
        )
    ).scalars().all()
    run_by_batch = {r.batch_id: r for r in runs}
    counts = dict(
        (
            await db.execute(
                select(ScraperConfig.batch_id, func.count())
                .where(
                    ScraperConfig.user_id == current_user.id,
                    ScraperConfig.batch_id.in_(batch_ids),
                )
                .group_by(ScraperConfig.batch_id)
            )
        ).all()
    )
    return [_summary(b, run_by_batch.get(b.id), counts.get(b.id, 0)) for b in batches]


@router.get("/{batch_id}", response_model=BatchDetailResponse)
async def get_batch(
    batch_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> BatchDetailResponse:
    """A batch's status + per-child (county x record_type) summary."""
    batch = await _owned_batch(db, batch_id, current_user.id)
    run = await _run_for(db, batch_id, current_user.id)

    # Map child config -> its dispatched job (scoped to THIS run's child_job_ids so
    # an unrelated job on the same config — should not happen in 2A — can't leak in).
    job_by_config: dict[str, tuple[str, str, int]] = {}
    if run and run.child_job_ids:
        job_rows = (
            await db.execute(
                select(Job.id, Job.scraper_config_id, Job.status, Job.record_count).where(
                    Job.user_id == current_user.id, Job.id.in_(run.child_job_ids)
                )
            )
        ).all()
        job_by_config = {scid: (jid, st, rc) for (jid, scid, st, rc) in job_rows}

    config_rows = (
        await db.execute(
            select(ScraperConfig.id, ScraperConfig.county, ScraperConfig.record_type)
            .where(
                ScraperConfig.batch_id == batch_id,
                ScraperConfig.user_id == current_user.id,
            )
            .order_by(ScraperConfig.county, ScraperConfig.record_type)
        )
    ).all()
    children = []
    for cid, county, record_type in config_rows:
        job = job_by_config.get(cid)
        children.append(
            BatchChildSummary(
                config_id=cid,
                county=county,
                record_type=record_type,
                job_id=job[0] if job else None,
                status=job[1] if job else "pending",
                record_count=job[2] if job else 0,
            )
        )

    return BatchDetailResponse(
        **_summary(batch, run, len(children)).model_dump(),
        failed_children=run.failed_children if run else None,
        children=children,
    )


@router.get("/{batch_id}/download")
async def download_batch(
    batch_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> StreamingResponse:
    """Stream the batch's combined CSV through this authed endpoint.

    Streams via DataExporter.download_object (the Cloudflare R2 REST API — the
    same path upload_to_r2 uses, proven in prod). We do NOT hand out an S3
    presigned URL: that path isn't valid in this R2 config and PII stays behind
    auth this way. Re-downloadable, so the file reflects later skip-trace fills.
    404 until the barrier has produced the combined CSV.
    """
    await _owned_batch(db, batch_id, current_user.id)  # 404s if not the owner
    run = await _run_for(db, batch_id, current_user.id)
    if run is None or not run.combined_export_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The combined CSV is not ready yet.",
        )
    from src.utils.data_exporter import DataExporter

    try:
        data = DataExporter().download_object(run.combined_export_key)
    except Exception as exc:  # R2 fetch failure — surface a clean 503
        _logger.error("batch %s download failed: %s", batch_id, str(exc)[:200])
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Export download is temporarily unavailable.",
        ) from exc
    filename = f"batch-{batch_id[:8]}.csv"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
