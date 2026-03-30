"""Scraper config routes: CRUD for user's scraper configurations."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import CurrentUser
from src.api.deps import get_rls_db
from src.api.schemas import (
    CachedRecordRow,
    CachedResultsPage,
    ConnectorCreate,
    ConnectorResponse,
    ScraperConfigCreate,
    ScraperConfigResponse,
)
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
            func.lower(CountyConnector.county) == body.county.lower(),
            func.upper(CountyConnector.state) == body.state.upper(),
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
    if body.enrichment.skip_tracing and current_user.plan not in _BUSINESS_PLANS:
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
        fields=body.fields.model_dump(),
        enrichment=body.enrichment.model_dump(),
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


# ─── Admin: County connector management ──────────────────────────────────────


@router.post("/connectors", response_model=ConnectorResponse, status_code=status.HTTP_201_CREATED)
async def create_connector(
    body: ConnectorCreate,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ConnectorResponse:
    """Add a new county connector. Agency plan only.

    For AI-mode connectors, no Python scraper code is needed — just provide
    the county portal URL and Claude handles the rest.
    """
    # Agency-only (admin feature)
    if current_user.plan != "agency":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Adding county connectors requires an Agency plan",
        )

    # Check for duplicate
    result = await db.execute(
        select(CountyConnector).where(
            CountyConnector.county == body.county,
            CountyConnector.state == body.state.lower(),
        )
    )
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Connector for {body.county}, {body.state} already exists",
        )

    connector = CountyConnector(
        id=str(uuid.uuid4()),
        county=body.county,
        state=body.state.lower(),
        record_types=body.record_types,
        scraper_class="src.scrapers.ai_scraper.AIScraper",
        scraper_mode=body.scraper_mode,
        base_url=body.base_url,
    )
    db.add(connector)
    await db.flush()
    return ConnectorResponse.model_validate(connector)


# ─── Cached records endpoint ─────────────────────────────────────────────────


@router.get("/{config_id}/records", response_model=CachedResultsPage)
async def get_cached_records(
    config_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    q: str | None = None,
):
    """Serve pre-scraped records from cache with per-user 'new' badges."""
    # 1. Verify config belongs to user
    config_result = await db.execute(
        select(ScraperConfig).where(
            ScraperConfig.id == config_id,
            ScraperConfig.user_id == current_user.id,
            ScraperConfig.active,
        )
    )
    config = config_result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=404, detail="Scraper config not found")

    county = config.county.lower()
    state = config.state.upper()

    # 2. Atomic: read old last_viewed_at, then update to NOW()
    old_view_result = await db.execute(
        text("""
            SELECT last_viewed_at FROM user_record_views
            WHERE user_id = :user_id AND scraper_config_id = :config_id
            FOR UPDATE
        """),
        {"user_id": current_user.id, "config_id": config_id},
    )
    old_row = old_view_result.fetchone()
    previous_viewed = old_row.last_viewed_at if old_row else None

    await db.execute(
        text("""
            INSERT INTO user_record_views (id, user_id, scraper_config_id, last_viewed_at)
            VALUES (gen_random_uuid(), :user_id, :config_id, NOW())
            ON CONFLICT (user_id, scraper_config_id)
            DO UPDATE SET last_viewed_at = NOW()
        """),
        {"user_id": current_user.id, "config_id": config_id},
    )

    # 3. Build doc_type filter from record_type keywords
    from src.scrapers.templates.eagleweb import _DOC_TYPE_MAP
    keywords = _DOC_TYPE_MAP.get(config.record_type, [])
    type_filter = ""
    type_params = {}
    if keywords:
        conditions = []
        for i, kw in enumerate(keywords):
            param_name = f"kw_{i}"
            conditions.append(f"doc_type ILIKE :{param_name}")
            type_params[param_name] = f"%{kw}%"
        type_filter = "AND (doc_type IS NULL OR " + " OR ".join(conditions) + ")"

    # 4. Search filter
    search_filter = ""
    if q and len(q) <= 100:
        from src.api.middleware.security import sanitize_search
        clean_q = sanitize_search(q)
        search_filter = "AND (party_name ILIKE :q OR property_address ILIKE :q OR parcel_id ILIKE :q)"
        type_params["q"] = f"%{clean_q}%"

    # 5. Count total + new_count
    count_sql = f"""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE scraped_at > COALESCE(:prev_viewed, '1970-01-01'::timestamptz)) AS new_count
        FROM county_records
        WHERE LOWER(county) = :county AND UPPER(state) = :state
        {type_filter} {search_filter}
    """
    counts = await db.execute(
        text(count_sql),
        {"county": county, "state": state, "prev_viewed": previous_viewed, **type_params},
    )
    count_row = counts.fetchone()
    total = count_row.total if count_row else 0
    new_count = count_row.new_count if count_row else 0

    # 6. Fetch paginated records
    offset = (page - 1) * page_size
    records_sql = f"""
        SELECT *,
            CASE WHEN scraped_at > COALESCE(:prev_viewed, '1970-01-01'::timestamptz) THEN true ELSE false END AS is_new
        FROM county_records
        WHERE LOWER(county) = :county AND UPPER(state) = :state
        {type_filter} {search_filter}
        ORDER BY scraped_at DESC
        LIMIT :limit OFFSET :offset
    """
    result = await db.execute(
        text(records_sql),
        {"county": county, "state": state, "prev_viewed": previous_viewed,
         "limit": page_size, "offset": offset, **type_params},
    )
    rows = result.fetchall()

    # 7. Cache age
    cache_age = None
    cache_stale = True
    if rows:
        latest_batch = await db.execute(
            text("SELECT MAX(batch_date) FROM county_records WHERE LOWER(county) = :county AND UPPER(state) = :state"),
            {"county": county, "state": state},
        )
        max_batch = latest_batch.scalar()
        if max_batch:
            age = datetime.now(UTC).date() - max_batch
            cache_age = f"{age.days}d" if age.days > 0 else "today"
            cache_stale = age.days > 1

    await db.commit()

    return CachedResultsPage(
        config_id=config_id,
        county=config.county,
        state=config.state,
        total=total,
        new_count=new_count,
        cache_age=cache_age,
        cache_stale=cache_stale,
        page=page,
        page_size=page_size,
        items=[
            CachedRecordRow(
                id=str(r.id),
                date_recorded=r.date_recorded,
                party_name=r.party_name,
                heirs=r.heirs,
                doc_type=r.doc_type,
                legal_description=r.legal_description,
                parcel_id=r.parcel_id,
                property_address=r.property_address,
                mailing_address=r.mailing_address,
                is_new=r.is_new,
                scraped_at=r.scraped_at,
            )
            for r in rows
        ],
    )
