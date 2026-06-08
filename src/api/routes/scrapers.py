"""Scraper config routes: CRUD for user's scraper configurations."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import CurrentUser
from src.api.deps import get_rls_db
from src.api.middleware.rate_limit import rate_limit
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
    """Public endpoint: returns precomputed, sanitized sample records.

    Reads ONLY the public_sample_cache singleton (refreshed hourly by the
    refresh_public_sample_cache Celery task, which does all PII redaction).
    This unauthenticated endpoint therefore never live-queries the tenant
    tables (results/jobs/scraper_configs) — so under the non-BYPASSRLS cutover
    role it needs no cross-tenant read access, and there is no path by which an
    anonymous caller can reach un-sanitized PII (RLS cutover Phase 2b, Codex
    design). Returns an empty-but-valid shape until the first refresh runs.
    """
    import json

    row = await db.execute(
        text("SELECT payload FROM public.public_sample_cache WHERE id = 1")
    )
    cached = row.scalar_one_or_none()
    if cached is None:
        # Cache not yet warmed (fresh DB / before the first beat tick).
        return {
            "records": [],
            "total_scraped": "0+",
            "counties_active": 0,
            "enrichment_rate": "95%+",
            "freshness": "Updated daily",
        }
    # asyncpg may hand back jsonb as a dict or a JSON string depending on codecs;
    # normalize both.
    return cached if isinstance(cached, dict) else json.loads(cached)


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

    # Phase 2b: validate optional pre-foreclosure document-type selection against
    # the capability registry (single source of truth). None = legacy/full output
    # (no validation needed). A non-empty list is only valid for pre_foreclosure
    # and must be available for this county; [] is rejected. Validation lives here
    # in the route (not in Pydantic) so scraper code isn't imported into schemas.
    if body.doc_types is not None:
        from src.scrapers.doc_types import validate_selection
        if body.record_type != "pre_foreclosure":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="doc_types is only valid for the pre_foreclosure record type",
            )
        ok, err = validate_selection(body.county, body.state, body.doc_types)
        if not ok:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=err)

    # Business+ feature gating. Phase 5: the dialer push is a second outbound
    # destination that POSTs lead PII, so it carries the SAME entitlement as the
    # job-summary webhook — gate both, or a lower plan could exfiltrate PII via
    # dialer_webhook_url (Codex).
    if (
        body.deliver.webhook_url or body.deliver.dialer_webhook_url
    ) and current_user.plan not in BUSINESS_FEATURES_PLANS:
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
        doc_types=body.doc_types,  # Phase 2b: None = legacy/full output
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
    from src.scrapers.doc_types import selectable_doc_type_labels
    out: list[ConnectorResponse] = []
    for c in result.scalars().all():
        resp = ConnectorResponse.model_validate(c)
        # Phase 2b: attach the pre-foreclosure doc-type selector options as a
        # {canonical_token: human_label} map — the exact shape the frontend
        # checkbox selector renders. Only where this county supports selection.
        if "pre_foreclosure" in (c.record_types or []):
            resp.pre_foreclosure_doc_types = selectable_doc_type_labels(c.county, c.state)
        out.append(resp)
    return out


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
    # Admin-only: return 404 (not 403) to non-admins so the endpoint's
    # existence isn't confirmed to non-admin callers (enumeration defense).
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not found",
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

    # gis_endpoint is fetched server-side during enrichment (county_gis). Run it
    # through the SSRF firewall here too so a private/metadata endpoint can't be
    # persisted. require_allowlisted=False: GIS hosts are varied public ArcGIS
    # servers, not on the scrape allowlist — the resolve + blocked-IP checks are
    # the gate. (Fetch-time safe_get is the authoritative guard for any path.)
    if body.gis_endpoint:
        from src.api.middleware.security import validate_scraping_target
        try:
            validate_scraping_target(body.gis_endpoint, require_allowlisted=False, resolve=True)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid gis_endpoint: {exc}",
            )

    # assessor_url is also fetched server-side (AI enrichment fallback), so
    # it must clear the same SSRF firewall as gis_endpoint before we persist
    # it. REDTEAM LOW N3 flagged that gis_endpoint/assessor_url were validated
    # (gis) but never stored, and assessor_url was neither validated nor
    # stored — dead input that silently dropped admin-supplied config.
    if body.assessor_url:
        from src.api.middleware.security import validate_scraping_target
        try:
            validate_scraping_target(body.assessor_url, require_allowlisted=False, resolve=True)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid assessor_url: {exc}",
            )

    connector = CountyConnector(
        id=str(uuid.uuid4()),
        county=body.county,
        state=body.state.upper(),
        record_types=body.record_types,
        scraper_class="src.scrapers.ai_scraper.AIScraper",
        scraper_mode=body.scraper_mode,
        base_url=body.base_url,
        # REDTEAM LOW N3: persist the validated enrichment endpoints so the
        # validation isn't dead — enrichment reads these off the connector row.
        gis_endpoint=body.gis_endpoint,
        assessor_url=body.assessor_url,
    )
    db.add(connector)
    await db.flush()
    return ConnectorResponse.model_validate(connector)


# ─── Cached records endpoint ─────────────────────────────────────────────────


@router.get("/{config_id}/records", response_model=CachedResultsPage)
async def get_cached_records(
    config_id: str,
    current_user: CurrentUser,
    request: Request,
    db: AsyncSession = Depends(get_rls_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    q: str | None = None,
):
    """Serve pre-scraped records from cache with per-user 'new' badges."""
    # Rate-limit before the cached-records query (FOR UPDATE + counts).
    await rate_limit(request, zone="general", identifier=current_user.id)
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


def _reset_failed_dialer_deliveries(job_id: str, user_id: str) -> int:
    """Reset this user's FAILED dialer outbox rows for a job back to pending.

    Runs in the system session (the dialer_deliveries table is worker-only, not
    granted to the app role) with an EXPLICIT user_id filter as the tenant guard.
    Only 'failed' rows are touched, never 'delivered' — a replay can't re-push a
    contact that already landed. Returns the number reset.
    """
    from sqlalchemy import update

    from src.db.models import DialerDelivery
    from src.db.session import system_sync_session

    with system_sync_session() as db:
        n = db.execute(
            update(DialerDelivery)
            .where(
                DialerDelivery.job_id == job_id,
                DialerDelivery.user_id == user_id,
                DialerDelivery.status == "failed",
            )
            .values(status="pending", last_error=None)
        ).rowcount
        db.commit()
    return int(n or 0)


@router.post("/{config_id}/jobs/{job_id}/dialer-replay")
async def replay_dialer_push(
    config_id: str,
    job_id: str,
    current_user: CurrentUser,
    request: Request,
    db: AsyncSession = Depends(get_rls_db),
) -> dict:
    """Re-queue FAILED native-dialer (outbox) deliveries for a job.

    Native bulk-less dialers (e.g. PhoneBurner) deliver one contact per POST, so a
    job can finish with some contacts failed (rate-limit/401/outage). This resets
    only the failed rows to pending and re-runs the outbox drain — delivered rows
    are never re-pushed. Tenant-scoped twice: the job must belong to the caller AND
    to this config (RLS read), and the reset filters by user_id (system session).
    """
    import anyio

    from src.db.models import Job

    await rate_limit(request, zone="general", identifier=current_user.id)

    job = (
        await db.execute(
            select(Job).where(
                Job.id == job_id,
                Job.user_id == current_user.id,
                Job.scraper_config_id == config_id,
            )
        )
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    reset = await anyio.to_thread.run_sync(
        _reset_failed_dialer_deliveries, job_id, current_user.id
    )
    if reset:
        from src.workers.dialer_outbox import process_dialer_outbox
        process_dialer_outbox.delay(job_id)
    return {"job_id": job_id, "replayed": reset}
