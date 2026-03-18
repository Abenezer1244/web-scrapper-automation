"""Scraper config routes: CRUD for user's scraper configurations."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import CurrentUser
from src.api.deps import get_rls_db
from src.api.schemas import ConnectorResponse, ScraperConfigCreate, ScraperConfigResponse
from src.db import CountyConnector, ScraperConfig, get_db

router = APIRouter(prefix="/scrapers", tags=["scrapers"])

_BUSINESS_PLANS = ("business", "agency")


@router.get("", response_model=list[ScraperConfigResponse])
async def list_scrapers(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> list[ScraperConfigResponse]:
    result = await db.execute(
        select(ScraperConfig)
        .where(ScraperConfig.user_id == current_user.id, ScraperConfig.active)
        .order_by(ScraperConfig.created_at.desc())
    )
    return [ScraperConfigResponse.model_validate(s) for s in result.scalars().all()]


@router.post("", response_model=ScraperConfigResponse, status_code=status.HTTP_201_CREATED)
async def create_scraper(
    body: ScraperConfigCreate,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> ScraperConfigResponse:
    # Verify county + record_type exists in the connector registry
    # county_connectors has no RLS — the rls db session can still query it
    result = await db.execute(
        select(CountyConnector).where(
            CountyConnector.county == body.county,
            CountyConnector.state == body.state,
            CountyConnector.active,
        )
    )
    connector = result.scalar_one_or_none()
    if connector is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"No active connector found for {body.county}, {body.state}",
        )
    if body.record_type not in connector.record_types:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Record type '{body.record_type}' not supported for {body.county}, {body.state}. "
                   f"Supported: {connector.record_types}",
        )

    # Business+ feature gating
    if body.deliver.webhook_url and current_user.plan not in _BUSINESS_PLANS:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Webhook delivery requires a Business or Agency plan",
        )
    if body.enrichment and "skip_tracing" in body.enrichment and current_user.plan not in _BUSINESS_PLANS:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Skip tracing enrichment requires a Business or Agency plan",
        )

    config = ScraperConfig(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        name=body.name,
        county=body.county,
        state=body.state,
        record_type=body.record_type,
        fields=body.fields,
        enrichment=body.enrichment,
        schedule=body.schedule.model_dump(),
        deliver=body.deliver.model_dump(),
    )
    db.add(config)
    await db.flush()
    return ScraperConfigResponse.model_validate(config)


@router.get("/connectors", response_model=list[ConnectorResponse])
async def list_connectors(
    db: AsyncSession = Depends(get_db),
) -> list[ConnectorResponse]:
    """Return all active county connectors. Used by the frontend county picker.

    Public endpoint — no auth required so the county browser loads for anonymous visitors.
    """
    result = await db.execute(
        select(CountyConnector)
        .where(CountyConnector.active)
        .order_by(CountyConnector.state, CountyConnector.county)
    )
    return [ConnectorResponse.model_validate(c) for c in result.scalars().all()]


@router.get("/{scraper_id}", response_model=ScraperConfigResponse)
async def get_scraper(
    scraper_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> ScraperConfigResponse:
    result = await db.execute(
        select(ScraperConfig).where(
            ScraperConfig.id == scraper_id,
            ScraperConfig.user_id == current_user.id,
        )
    )
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scraper not found")
    return ScraperConfigResponse.model_validate(config)


@router.delete("/{scraper_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scraper(
    scraper_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> None:
    result = await db.execute(
        select(ScraperConfig).where(
            ScraperConfig.id == scraper_id,
            ScraperConfig.user_id == current_user.id,
        )
    )
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scraper not found")
    config.active = False  # Soft delete — preserves job history
    await db.flush()
