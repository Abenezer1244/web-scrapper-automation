"""Batch scrape routes (Piece 2): create a multi-county x multi-record-type batch.

A batch fans out into N ordinary scrapes (one child ScraperConfig per county x
record_type) under a parent ScraperBatch holding the shared
fields/enrichment/deliver. The batch owns delivery (one combined CSV) — child
delivery + schedule are SUPPRESSED. The BatchRun + child Jobs are created async
by the dispatch worker (system-written), so this route only persists the parent
+ children, then kicks off the fan-out.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import CurrentUser
from src.api.deps import get_rls_db
from src.api.middleware.rate_limit import rate_limit
from src.api.schemas import BatchCreateRequest, BatchCreateResponse
from src.config.constants import (
    BATCH_HARD_CEILING,
    BATCH_MAX_COMBINATIONS,
    BATCH_PLANS,
    BUSINESS_FEATURES_PLANS,
    SKIP_TRACE_ADDON_PLANS,
)
from src.db import CountyConnector
from src.db.models import ScraperBatch, ScraperConfig
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
