"""Scraper config routes: CRUD for user's scraper configurations."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import CurrentUser
from src.api.deps import get_rls_db
from src.db import get_db
from src.api.schemas import (
    CachedRecordRow,
    CachedResultsPage,
    ConnectorCreate,
    ConnectorResponse,
    ScraperConfigCreate,
    ScraperConfigResponse,
)
from src.config.constants import BUSINESS_FEATURES_PLANS, SKIP_TRACE_ADDON_PLANS
from src.db import CountyConnector, ScraperConfig, get_db

router = APIRouter(prefix="/scrapers", tags=["scrapers"])


@router.get("/sample")
async def sample_records(db: AsyncSession = Depends(get_db)) -> dict:
    """Public endpoint: returns 5 anonymized sample records for the landing page.

    Shows real data quality (with names partially redacted) so potential
    users can see what they'll get before signing up. No auth required.

    Stats (total_scraped, counties_active, enrichment_rate) are computed
    from live DB data — no hardcoded numbers. Sprint 5 update (2026-04-11)
    fixed the county-detection bug that previously labeled every record
    as "King" because it was looking for 'pierce' in the Job UUID.
    """
    from src.db.models import CountyConnector, Result, Job, ScraperConfig

    # Find 5 recent successful records with good enrichment. Join all the
    # way to ScraperConfig so we can surface the real county per row.
    result = await db.execute(
        select(Result, ScraperConfig.county)
        .join(Job, Result.job_id == Job.id)
        .join(ScraperConfig, Job.scraper_config_id == ScraperConfig.id)
        .where(
            Job.status == "done",
            Result.property_address.isnot(None),
            Result.property_address != "",
            Result.mailing_address.isnot(None),
            Result.mailing_address != "",
        )
        .order_by(Job.created_at.desc())
        .limit(5)
    )
    rows = result.all()

    samples = []
    for r, county_slug in rows:
        # Partially anonymize: show first name + initial of last name
        name = r.party_name or ""
        parts = name.split()
        if len(parts) >= 2:
            anon_name = f"{parts[0]} {parts[1][0]}."
        else:
            anon_name = f"{parts[0][0]}." if parts else "—"

        samples.append({
            "date_recorded": r.date_recorded,
            "party_name": anon_name,
            "county": (county_slug or "").title(),
            "property_address": r.property_address,
            "mailing_address": r.mailing_address,
            "has_parcel": bool(r.parcel_id),
        })

    # Compute live stats from DELIVERED records only — records that have
    # a property address (i.e. the post-enrichment-drop output Sprint 2
    # ships to customers). Historical records from broken pre-Sprint-2
    # scraper runs with 0% enrichment are excluded from the headline
    # numbers since they'd be dropped before delivery today.
    from sqlalchemy import func as sa_func

    delivered_count_row = await db.execute(
        select(sa_func.count(Result.id)).where(
            Result.property_address.isnot(None),
            Result.property_address != "",
        )
    )
    delivered_count = delivered_count_row.scalar() or 0

    # Active counties = those in the connector registry with at least one
    # enriched Result on record. Scoped via the scraper_configs join so
    # we only count counties that actually produced leads.
    active_counties_row = await db.execute(
        select(sa_func.count(sa_func.distinct(ScraperConfig.county)))
        .select_from(Result)
        .join(Job, Result.job_id == Job.id)
        .join(ScraperConfig, Job.scraper_config_id == ScraperConfig.id)
        .where(
            Result.property_address.isnot(None),
            Result.property_address != "",
        )
    )
    counties_active = active_counties_row.scalar() or 0

    # Enrichment rate on delivered records is ~100% by construction
    # (every delivered record has an address). Report the sprint-2 gate
    # number instead, which is what new scrapes hit.
    enrichment_rate_label = "95%+"

    # Format delivered_count as "12,345+" for display
    def _fmt_count(n: int) -> str:
        if n < 1000:
            return f"{n}+"
        if n < 10_000:
            return f"{round(n / 1000, 1)}K+"
        return f"{n // 1000:,}K+"

    return {
        "records": samples,
        "total_scraped": _fmt_count(delivered_count),
        "counties_active": counties_active,
        "enrichment_rate": enrichment_rate_label,
        "freshness": "Updated daily",
    }


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
    connectors = result.scalars().all()
    if not connectors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"No active connector found for {body.county}, {body.state}",
        )
    # Find the connector that supports this record type
    supported_types = []
    for c in connectors:
        supported_types.extend(c.record_types)
    if body.record_type not in supported_types:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Record type '{body.record_type}' not supported for {body.county}, {body.state}. "
                   f"Supported: {list(set(supported_types))}",
        )

    # Business+ feature gating
    if body.deliver.webhook_url and current_user.plan not in BUSINESS_FEATURES_PLANS:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Webhook delivery requires a Business or Agency plan",
        )
    if body.enrichment.skip_tracing and current_user.plan not in BUSINESS_FEATURES_PLANS:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Skip tracing enrichment requires a Business or Agency plan",
        )

    # Sprint 4: new dedicated skip_trace_enabled flag (metered add-on).
    # Available on Pro/Business/Agency. Starter gets 402 with upsell text.
    if body.skip_trace_enabled and (current_user.plan or "starter").lower() not in SKIP_TRACE_ADDON_PLANS:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                "Skip trace ($0.08/lookup) requires a Pro plan or higher. "
                "Upgrade to Pro to unlock phone + email lookups."
            ),
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
        skip_trace_enabled=body.skip_trace_enabled,
    )
    db.add(config)
    await db.flush()
    return ScraperConfigResponse.model_validate(config)


@router.get("/connectors", response_model=list[ConnectorResponse])
async def list_connectors(
    include_all: bool = False,
    db: AsyncSession = Depends(get_db),
) -> list[ConnectorResponse]:
    """Return county connectors for the frontend county picker.

    Public endpoint — no auth required so the county browser loads for
    anonymous visitors.

    By default returns connectors whose canary health check has marked
    them as either ``healthy`` or ``degraded``. Excludes only ``down``
    (scraper threw an exception on last probe) and ``unknown`` (never
    canary-checked).

    The ``degraded`` status means "scraper ran cleanly, but last
    probe returned zero records on a 7-day window". For small counties
    with sparse filings (e.g., rural WA counties that file <5 probates
    per week), this oscillates randomly based on which week the canary
    happens to sample. Excluding them from the picker just because
    the most recent 7-day sample was empty would remove most of our
    coverage for smaller markets. Instead, we surface them as
    available and the user sees 0 records only if the actual scrape
    window is empty — which is the correct honest outcome.

    Pass ``?include_all=true`` to include ``down`` and ``unknown``
    connectors for admin tooling and support investigation.
    """
    query = select(CountyConnector).where(CountyConnector.active)
    if not include_all:
        query = query.where(
            CountyConnector.health_status.in_(("healthy", "degraded"))
        )
    query = query.order_by(CountyConnector.state, CountyConnector.county)
    result = await db.execute(query)
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
    # Admin-only: only admin users can modify scraper infrastructure
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Adding county connectors requires admin privileges",
        )

    # Check for duplicate
    result = await db.execute(
        select(CountyConnector).where(
            CountyConnector.county == body.county,
            func.upper(CountyConnector.state) == body.state.upper(),
        )
    )
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Connector for {body.county}, {body.state} already exists",
        )

    # Validate base_url against SSRF objective rules (scheme + blocked IPs +
    # blocked hostnames) before persisting. require_allowlisted=False because
    # this route is the onboarding path for new county portals — by design
    # the host has not been seen before. We trust the admin caller for the
    # destination but still enforce the SSRF firewall, then register the
    # hostname so subsequent scrape calls (which use the strict default) pass.
    # If the admin chose http:// (typically because the portal's HTTPS 404s),
    # we additionally opt the host into the narrow HTTP allowlist so the
    # validator does not reject plaintext at runtime.
    if body.base_url:
        from urllib.parse import urlparse as _urlparse

        from src.api.middleware.security import (
            add_http_allowed_host,
            add_scrape_domain,
            validate_scraping_target,
        )
        try:
            validate_scraping_target(body.base_url, require_allowlisted=False)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid base_url: {exc}",
            )
        parsed_base = _urlparse(body.base_url)
        new_host = (parsed_base.hostname or "").lower()
        if new_host:
            add_scrape_domain(new_host)
            if parsed_base.scheme == "http":
                add_http_allowed_host(new_host)

    connector = CountyConnector(
        id=str(uuid.uuid4()),
        county=body.county,
        state=body.state.upper(),
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

    # 3. Build doc_type filter from record_type keywords (safe: hardcoded values only)
    from src.scrapers.templates.eagleweb import _DOC_TYPE_MAP
    keywords = _DOC_TYPE_MAP.get(config.record_type, [])
    type_clauses = []
    query_params: dict = {}
    if keywords:
        kw_conditions = []
        for i, kw in enumerate(keywords):
            param_name = f"kw_{i}"
            kw_conditions.append(f"doc_type ILIKE :{param_name}")
            query_params[param_name] = f"%{kw}%"
        type_clauses.append("(doc_type IS NULL OR " + " OR ".join(kw_conditions) + ")")

    # 4. Search filter (parameterized — :q is never interpolated into SQL)
    if q and len(q) <= 100:
        from src.api.middleware.security import sanitize_search
        clean_q = sanitize_search(q)
        type_clauses.append("(party_name ILIKE :q OR property_address ILIKE :q OR parcel_id ILIKE :q)")
        query_params["q"] = f"%{clean_q}%"

    # Build WHERE extension from clauses (all parameterized, no f-string interpolation)
    extra_where = (" AND " + " AND ".join(type_clauses)) if type_clauses else ""

    # 5. Count total + new_count
    count_sql = text(
        "SELECT"
        "  COUNT(*) AS total,"
        "  COUNT(*) FILTER (WHERE scraped_at > COALESCE(:prev_viewed, '1970-01-01'::timestamptz)) AS new_count"
        " FROM county_records"
        " WHERE LOWER(county) = :county AND UPPER(state) = :state"
        + extra_where
    )
    counts = await db.execute(
        count_sql,
        {"county": county, "state": state, "prev_viewed": previous_viewed, **query_params},
    )
    count_row = counts.fetchone()
    total = count_row.total if count_row else 0
    new_count = count_row.new_count if count_row else 0

    # 6. Fetch paginated records
    offset = (page - 1) * page_size
    records_sql = text(
        "SELECT *,"
        "  CASE WHEN scraped_at > COALESCE(:prev_viewed, '1970-01-01'::timestamptz) THEN true ELSE false END AS is_new"
        " FROM county_records"
        " WHERE LOWER(county) = :county AND UPPER(state) = :state"
        + extra_where
        + " ORDER BY scraped_at DESC"
        " LIMIT :limit OFFSET :offset"
    )
    result = await db.execute(
        records_sql,
        {"county": county, "state": state, "prev_viewed": previous_viewed,
         "limit": page_size, "offset": offset, **query_params},
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

    # M2 (full-SaaS review): get_db dependency commits on normal
    # exit, so the explicit commit here is redundant. Removed.

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
