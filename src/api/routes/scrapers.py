"""Scraper config routes: CRUD for user's scraper configurations."""

import re
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import CurrentUser, require_admin_mfa
from src.api.deps import get_rls_db
from src.api.entitlements import enforce_entitlements
from src.api.middleware.rate_limit import rate_limit
from src.api.middleware.security import audit_log
from src.api.schemas import (
    DELIVER_SECRET_FIELDS,
    CachedRecordRow,
    CachedResultsPage,
    ConnectorCreate,
    ConnectorResponse,
    DeliverConfig,
    ScraperConfigCreate,
    ScraperConfigResponse,
    ScraperConfigUpdate,
)
from src.config.constants import (
    ACTIVE_STATUSES,
    BUSINESS_FEATURES_PLANS,
    SKIP_TRACE_ADDON_PLANS,
)
from src.db import CountyConnector, ScraperConfig, get_db
from src.scrapers.probate import (
    effective_tod_on_update,
    new_probate_config_tod_default,
)

router = APIRouter(prefix="/scrapers", tags=["scrapers"])

# Phase 3: the living-owner TOD toggle is a probate-only product control.
_TOD_TOGGLE_RECORD_TYPE = "probate"


def _validate_tod_toggle(record_type: str, include_living_owner_tod: bool | None) -> None:
    """422 if include_living_owner_tod is explicitly set on a non-probate config.

    Mirrors _validate_doc_types: the flag only governs probate output, so an explicit
    value on any other record type is a client error, not a silent no-op."""
    if include_living_owner_tod is not None and record_type != _TOD_TOGGLE_RECORD_TYPE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="include_living_owner_tod is only valid for the probate record type",
        )


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


async def _validate_connector_supports(
    db: AsyncSession, county: str, state: str, record_type: str
) -> None:
    """Identity validation: an active connector for county+state exists and
    supports record_type. Create-time only — on edit the identity is immutable
    (validated once at create), and re-checking would falsely 422 an unrelated
    edit (e.g. a rename) if the connector was later deactivated (Codex). Raises
    422 on failure.
    """
    # county_connectors has no RLS — the rls db session can still query it.
    result = await db.execute(
        select(CountyConnector).where(
            func.lower(CountyConnector.county) == county.lower(),
            func.upper(CountyConnector.state) == state.upper(),
            CountyConnector.active,
        )
    )
    connectors = result.scalars().all()
    if not connectors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"No active connector found for {county}, {state}",
        )
    supported_types = []
    for c in connectors:
        supported_types.extend(c.record_types)
    if record_type not in supported_types:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Record type '{record_type}' not supported for {county}, {state}. "
                   f"Supported: {list(set(supported_types))}",
        )


def _validate_doc_types(
    county: str, state: str, record_type: str, doc_types: list[str]
) -> None:
    """Phase 2b: validate a pre-foreclosure document-type selection against the
    capability registry (single source of truth). Only valid for pre_foreclosure
    and must be available for this county; [] is rejected. Shared by create and
    edit so the gate can't drift. Validation lives here (not in Pydantic) so
    scraper code isn't imported into schemas. Raises 422 on failure.
    """
    from src.scrapers.doc_types import validate_selection
    if record_type != "pre_foreclosure":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="doc_types is only valid for the pre_foreclosure record type",
        )
    ok, err = validate_selection(county, state, doc_types)
    if not ok:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=err)


def _enforce_plan_feature_gates(
    current_user,
    *,
    has_webhook: bool,
    skip_tracing: bool,
    skip_trace_enabled: bool,
) -> None:
    """Plan/tier gates for the gated delivery + enrichment features. Shared by
    create and edit so they can't drift. Create passes the payload's ABSOLUTE
    values; edit passes the ENABLE-DELTA (only the features this PATCH is newly
    turning on) so an edit can neither bypass a tier nor punish a config whose
    owner downgraded after creating it (the feature stays grandfathered until the
    user toggles it). Raises 402.
    """
    # Phase 5: the dialer push is a second outbound destination that POSTs lead
    # PII, so it carries the SAME entitlement as the job-summary webhook — gate
    # both, or a lower plan could exfiltrate PII via dialer_webhook_url (Codex).
    if has_webhook and current_user.plan not in BUSINESS_FEATURES_PLANS:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Webhook delivery requires a Business or Agency plan",
        )
    if skip_tracing and current_user.plan not in BUSINESS_FEATURES_PLANS:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Skip tracing enrichment requires a Business or Agency plan",
        )
    # Sprint 4: dedicated skip_trace_enabled flag (metered add-on). Available on
    # Pro/Business/Agency. Starter gets 402 with upsell text.
    if skip_trace_enabled and (current_user.plan or "starter").lower() not in SKIP_TRACE_ADDON_PLANS:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                "Skip trace ($0.08/lookup) requires a Pro plan or higher. "
                "Upgrade to Pro to unlock phone + email lookups."
            ),
        )


async def _build_scraper_config(
    db: AsyncSession,
    current_user,
    body: ScraperConfigCreate,
    request: Request,
) -> ScraperConfig:
    """Validate the payload (connector/record-type, entitlements, doc-types, plan
    gates) via the shared helpers and persist a ScraperConfig, then return it
    (flushed, not committed). Used by POST /scrapers; PATCH reuses the same
    validation helpers, so create and edit can't drift.

    The config is always ``active=True`` (visible on the dashboard, usable). Whether
    it auto-runs is governed solely by ``schedule.frequency``: the dispatcher skips
    ``frequency == "manual"`` (see scheduler_helpers/dispatch.py), so a manual config
    is visible but never fires on the beat. "active" = visible/usable; "frequency" =
    recurrence — the two are independent.
    """
    await _validate_connector_supports(db, body.county, body.state, body.record_type)

    # Value-metric entitlement gate (county count + record-type). Feature-flagged:
    # 402 when settings.ENTITLEMENT_ENFORCEMENT is on, else audit-log only.
    await enforce_entitlements(
        db,
        current_user,
        state=body.state,
        counties={body.county},
        record_types={body.record_type},
        context="create_scraper",
    )

    # None = legacy/full output (no validation needed).
    if body.doc_types is not None:
        _validate_doc_types(body.county, body.state, body.record_type, body.doc_types)

    # Phase 3: reject the TOD toggle on non-probate; resolve the NEW-config default
    # (probate + omitted/null -> False = exclude living-owner TOD).
    _validate_tod_toggle(body.record_type, body.include_living_owner_tod)
    eff_include_living_owner_tod = new_probate_config_tod_default(
        body.record_type, body.include_living_owner_tod
    )

    _enforce_plan_feature_gates(
        current_user,
        # A native dialer connector (dialer_type, e.g. phoneburner) pushes lead PII
        # even without a dialer_webhook_url, so it is gated like the webhooks (Codex)
        # — otherwise a non-Business user could save a config the worker then refuses
        # to run.
        has_webhook=bool(
            body.deliver.webhook_url
            or body.deliver.dialer_webhook_url
            or body.deliver.dialer_type
        ),
        skip_tracing=body.enrichment.skip_tracing,
        skip_trace_enabled=body.skip_trace_enabled,
    )

    schedule = body.schedule.model_dump()
    config = ScraperConfig(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        name=body.name,
        county=body.county,
        state=body.state,
        record_type=body.record_type,
        fields=body.fields.model_dump(),
        enrichment=body.enrichment.model_dump(),
        schedule=schedule,
        deliver=body.deliver.model_dump(),
        skip_trace_enabled=body.skip_trace_enabled,
        doc_types=body.doc_types,  # Phase 2b: None = legacy/full output
        include_living_owner_tod=eff_include_living_owner_tod,  # Phase 3
        active=True,  # visible/usable; recurrence is governed by schedule.frequency
    )
    db.add(config)
    await db.flush()
    return config


def _audit_config(request: Request, action: str, user_id: str, config: ScraperConfig) -> None:
    """Audit a scraper-config creation. Called AFTER all gates pass so a 402 /
    failed commit can never leave a false 'created' audit record (audit_log commits
    its own transaction, so it must fire only on the success path — Codex)."""
    audit_log(
        request, action, user_id,
        f"config_id={config.id} county={config.county}/{config.state} "
        f"type={config.record_type}",
    )


@router.post("", response_model=ScraperConfigResponse, status_code=status.HTTP_201_CREATED)
async def create_scraper(
    body: ScraperConfigCreate,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> ScraperConfigResponse:
    """Create a scraper config and return it. The frontend then POSTs /jobs to kick
    off the first run ("Start run"). The config is always active (visible on the
    dashboard); it only auto-runs if its schedule.frequency is recurring (the
    dispatcher skips frequency="manual")."""
    config = await _build_scraper_config(db, current_user, body, request)
    _audit_config(request, "scraper_created", current_user.id, config)
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
    from src.scrapers.doc_types import selectable_availability, selectable_doc_type_labels
    from src.scrapers.registry import connector_scraper_class
    out: list[ConnectorResponse] = []
    for c in result.scalars().all():
        resp = ConnectorResponse.model_validate(c)
        # Phase 2b: attach the pre-foreclosure doc-type selector options as a
        # {canonical_token: human_label} map — the exact shape the frontend
        # checkbox selector renders. Only where this county supports selection.
        if "pre_foreclosure" in (c.record_types or []):
            resp.pre_foreclosure_doc_types = selectable_doc_type_labels(c.county, c.state)
            # Phase B: surface method/confidence so the UI can honestly distinguish a
            # server-side portal filter ("verified") from a client-side text match
            # ("keyword"). None when the county isn't selectable.
            _sel = selectable_availability(c.county, c.state)
            if _sel is not None:
                resp.pre_foreclosure_doc_type_method = _sel.get("method")
                resp.pre_foreclosure_doc_type_confidence = _sel.get("confidence")
        # SHOW (read-only): what document types / dataset this connector collects,
        # per record type, for the wizard's "documents collected" display. Derived
        # from each scraper's own collection_scope(); None when nothing declared.
        scraper_cls = connector_scraper_class(c)
        if scraper_cls is not None:
            scopes: dict[str, dict] = {}
            for rt in (c.record_types or []):
                scope = scraper_cls.collection_scope(rt)
                if scope is not None:
                    scopes[rt] = scope.to_api()
            resp.collection_scope_by_record_type = scopes or None
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
    request: Request,
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
    # M7: scraper-config changes were unaudited (audit checklist finding).
    audit_log(
        request, "scraper_deleted", current_user.id,
        f"config_id={config.id} county={config.county}/{config.state}",
    )


def _is_secret_placeholder(value: str) -> bool:
    """A value made up only of asterisks is a redaction placeholder echoed back
    from a UI, not a real secret. Treat it as 'keep stored' so a PATCH never
    persists '******' over a real secret (Codex)."""
    s = value.strip()
    return bool(s) and set(s) == {"*"}


def _merge_deliver(stored: dict, update) -> DeliverConfig:
    """Build the effective delivery config for an edit.

    Write-only secrets (webhook_secret / dialer_webhook_secret /
    phoneburner_access_token) are PRESERVED when the client leaves them blank
    (omitted / null / blank / a '****' placeholder) and REPLACED only when a real
    new value arrives. Non-secret fields fully replace. When a destination URL (or
    the PhoneBurner dialer) is removed, its now-orphaned secret is dropped so it
    can't be silently reused if the destination is re-added later (Codex).

    The merged dict is run through a full DeliverConfig so EVERY delivery validator
    (URL scheme, secret length, dialer allowlist, require-connector-credentials)
    fires on the result — we never model_dump() a None over a stored secret.
    """
    from pydantic import ValidationError

    merged = update.model_dump()
    for secret in DELIVER_SECRET_FIELDS:
        new_val = merged.get(secret)
        if not (isinstance(new_val, str) and new_val.strip()) or _is_secret_placeholder(new_val):
            merged[secret] = stored.get(secret)  # keep stored
    # Drop orphaned secrets when their destination is gone.
    if not merged.get("webhook_url"):
        merged["webhook_secret"] = None
    if not merged.get("dialer_webhook_url"):
        merged["dialer_webhook_secret"] = None
    if merged.get("dialer_type") != "phoneburner":
        merged["phoneburner_access_token"] = None
        merged["phoneburner_owner_id"] = None
    try:
        return DeliverConfig(**merged)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"deliver: {first.get('msg', 'invalid delivery config')}",
        )


@router.patch("/{scraper_id}", response_model=ScraperConfigResponse)
async def update_scraper(
    scraper_id: str,
    body: ScraperConfigUpdate,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> ScraperConfigResponse:
    """Edit an existing scraper config in place (PATCH). Keeps the config id, so
    job history and a since_last_run cursor survive — unlike delete+recreate,
    which orphans history and resets the cursor.

    Editable: name, fields, enrichment, schedule, deliver, skip_trace_enabled,
    doc_types. Identity (county/state/record_type) is immutable. Each PROVIDED
    top-level field FULLY REPLACES the stored value (sub-objects are not
    deep-merged — the edit wizard pre-fills the complete object); an OMITTED field
    is kept. Blank delivery secrets are kept (see _merge_deliver).
    """
    from src.db.models import Job

    # 1. Identity is immutable. county/state/record_type exist on the model only so
    #    we can reject an attempt to change them with a clear 422 rather than
    #    silently ignoring it (Codex P2). model_fields_set so an explicit null trips
    #    it too.
    locked = {"county", "state", "record_type"} & body.model_fields_set
    if locked:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Cannot change identity field(s): "
                f"{', '.join(sorted(locked))}. County, state, and record_type are "
                "fixed for a scraper — create a new one instead."
            ),
        )

    # 1b. Reject an explicit null on a non-nullable editable field (Codex P2). These
    #     fields have no "unset" state — omit them to keep the stored value. Silently
    #     treating null like omission would contradict the provided-vs-omitted
    #     contract and mislead generated clients. (doc_types is genuinely nullable —
    #     null there means "legacy/full output" — so it is intentionally excluded.)
    explicit_nulls = sorted(
        f
        for f in ("name", "fields", "enrichment", "schedule", "deliver", "skip_trace_enabled")
        if f in body.model_fields_set and getattr(body, f) is None
    )
    if explicit_nulls:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Field(s) cannot be null: {', '.join(explicit_nulls)}. Omit them to keep the current value.",
        )

    # 2. Load the OWNED, ACTIVE config. FOR UPDATE serializes concurrent edits on
    #    the same row (belt for the updated_at token below). Soft-deleted configs
    #    are not editable (active filter → 404). RLS is the belt, user_id the
    #    suspenders.
    config = (
        await db.execute(
            select(ScraperConfig)
            .where(
                ScraperConfig.id == scraper_id,
                ScraperConfig.user_id == current_user.id,
                ScraperConfig.active,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scraper not found")

    # 2b. Batch children are owned by their parent batch, which suppresses their
    #     schedule + delivery (Codex P1). Editing one directly would let a user set
    #     a recurring schedule on a child (so the beat fires it independently of the
    #     batch) or drift its fields/enrichment from siblings. Reject — batch config
    #     is edited at the batch level, not here.
    if config.batch_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This scraper belongs to a batch; edit it from its batch, not individually.",
        )

    # 3. Optimistic concurrency (Codex P1): the client echoes verbatim the
    #    updated_at it read from GET; 409 unless it still EXACTLY equals the stored
    #    token (compared as the same UTC instant, full microsecond precision). No
    #    precision is discarded, so two commits in the same millisecond can't false-
    #    match — the contract is "send back the exact string GET returned". (A true
    #    version counter would be even stronger but needs a migration; deferred.)
    stored_ts = config.updated_at
    client_ts = body.updated_at
    if stored_ts.tzinfo is None:
        stored_ts = stored_ts.replace(tzinfo=UTC)
    if client_ts.tzinfo is None:
        client_ts = client_ts.replace(tzinfo=UTC)
    if stored_ts != client_ts:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This scraper was changed elsewhere. Reload and re-apply your edits.",
        )

    # 4. In-flight guard (Codex P1): the worker re-reads the config at runtime, so
    #    editing while a job is active would corrupt that job's fields/deliver/
    #    enrichment output. Block until it finishes or is cancelled.
    #
    #    Start/edit race: the FOR UPDATE lock above (held until this txn commits)
    #    closes it for same-DB job creation. Every execution path inserts a `jobs`
    #    row that FK-references this config, and a child INSERT takes a FOR KEY SHARE
    #    lock on the parent row — which CONFLICTS with our FOR UPDATE. So a job can't
    #    be created mid-edit: a concurrent insert blocks until we commit (then it
    #    reads the NEW config), and a job created first is seen by the check below
    #    (→ 409). The plan's durable fix (snapshot the execution config onto the Job
    #    at creation) is still worthwhile for cross-cutting safety but is not needed
    #    to make THIS endpoint correct. Batch children (which the dispatcher inserts)
    #    are rejected at step 2b.
    active_job = (
        await db.execute(
            select(Job.id)
            .where(
                Job.scraper_config_id == scraper_id,
                Job.user_id == current_user.id,
                Job.status.in_(ACTIVE_STATUSES),
            )
            .limit(1)
        )
    ).first()
    if active_job is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Cannot edit while a job is running for this scraper. "
                "Wait for it to finish or cancel it, then try again."
            ),
        )

    fields_set = body.model_fields_set
    stored_deliver = dict(config.deliver or {})

    # 5. Resolve effective values: a provided field replaces, an omitted one keeps
    #    the stored value. Sub-objects are replace-whole (validated at the request
    #    boundary); deliver is merged so blank secrets survive.
    if "name" in fields_set:
        if not (body.name and body.name.strip()):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="name cannot be empty",
            )
        eff_name = body.name.strip()
    else:
        eff_name = config.name

    eff_fields = body.fields.model_dump() if body.fields is not None else config.fields
    eff_enrichment = body.enrichment.model_dump() if body.enrichment is not None else config.enrichment
    eff_schedule = body.schedule.model_dump() if body.schedule is not None else config.schedule
    eff_skip_trace = (
        bool(body.skip_trace_enabled)
        if ("skip_trace_enabled" in fields_set and body.skip_trace_enabled is not None)
        else bool(config.skip_trace_enabled)
    )
    eff_doc_types = body.doc_types if ("doc_types" in fields_set) else config.doc_types
    # Phase 3: OMITTED preserves the stored value (an old None config stays
    # grandfathered — editing it must not silently flip TOD off); PRESENT replaces it.
    # An EXPLICIT null is treated as omitted (None is not a user-selectable state), so a
    # `"include_living_owner_tod": null` PATCH cannot downgrade a False/True probate
    # config back to grandfathered/include-all without an explicit `true` opt-in (Codex P2).
    eff_include_living_owner_tod = effective_tod_on_update(
        "include_living_owner_tod" in fields_set,
        body.include_living_owner_tod,
        config.include_living_owner_tod,
    )

    if "deliver" in fields_set and body.deliver is not None:
        eff_deliver = _merge_deliver(stored_deliver, body.deliver).model_dump()
    else:
        eff_deliver = stored_deliver

    # 6. Re-validate the doc-type selection only when it actually changes — the
    #    connector/record_type are immutable and were validated at create, so
    #    re-checking them would falsely 422 an unrelated edit if the connector was
    #    later deactivated (Codex).
    if "doc_types" in fields_set and eff_doc_types is not None:
        _validate_doc_types(config.county, config.state, config.record_type, eff_doc_types)

    # Phase 3: reject the TOD toggle when newly set on a non-probate config
    # (record_type is immutable, so the stored type is authoritative).
    if "include_living_owner_tod" in fields_set:
        _validate_tod_toggle(config.record_type, body.include_living_owner_tod)

    # 7. Plan/tier gates on the ENABLE-DELTA only: block a PATCH that newly turns
    #    on a gated feature (so a downgraded user can't bypass tiers), but
    #    grandfather a feature that was already on (so they can still rename, etc.).
    stored_enrichment = config.enrichment if isinstance(config.enrichment, dict) else {}
    eff_enrichment_dict = eff_enrichment if isinstance(eff_enrichment, dict) else {}
    # Gate each outbound destination SEPARATELY (Codex P2): the webhook, the dialer
    # webhook, and a native dialer connector (e.g. phoneburner) each independently
    # send lead PII, so a downgraded user who already has one on must still be gated
    # when ADDING another — collapsing them into one boolean would let the new one
    # slip through and save a Business-only config the worker then refuses to run.
    webhook_added = bool(eff_deliver.get("webhook_url")) and not bool(stored_deliver.get("webhook_url"))
    dialer_url_added = bool(eff_deliver.get("dialer_webhook_url")) and not bool(stored_deliver.get("dialer_webhook_url"))
    dialer_native_added = bool(eff_deliver.get("dialer_type")) and not bool(stored_deliver.get("dialer_type"))
    _enforce_plan_feature_gates(
        current_user,
        has_webhook=webhook_added or dialer_url_added or dialer_native_added,
        skip_tracing=bool(eff_enrichment_dict.get("skip_tracing")) and not bool(stored_enrichment.get("skip_tracing")),
        skip_trace_enabled=eff_skip_trace and not bool(config.skip_trace_enabled),
    )

    # 8. Detect the changed fields (full value compare, incl. secret values so a
    #    secret-only replace still counts). A no-op PATCH must not bump the
    #    concurrency token or emit an audit record (Codex).
    changed_fields: list[str] = []
    if eff_name != config.name:
        changed_fields.append("name")
    if eff_fields != config.fields:
        changed_fields.append("fields")
    if eff_enrichment != config.enrichment:
        changed_fields.append("enrichment")
    if eff_schedule != config.schedule:
        changed_fields.append("schedule")
    if eff_deliver != stored_deliver:
        changed_fields.append("deliver")
    if eff_skip_trace != bool(config.skip_trace_enabled):
        changed_fields.append("skip_trace_enabled")
    if eff_doc_types != config.doc_types:
        changed_fields.append("doc_types")
    if eff_include_living_owner_tod != config.include_living_owner_tod:
        changed_fields.append("include_living_owner_tod")

    if not changed_fields:
        return ScraperConfigResponse.model_validate(config)

    # Build the audit detail BEFORE mutating: changed field names, plus old/new for
    # the small non-secret scalars. Dict fields (fields/enrichment/schedule/deliver)
    # are logged by name only — deliver carries write-only secrets that must never
    # be logged (Codex), and the others are verbose booleans/dates.
    audit_bits = [f"config_id={config.id}", "changed=" + ",".join(sorted(changed_fields))]
    if "name" in changed_fields:
        audit_bits.append(f"name={config.name!r}->{eff_name!r}")
    if "skip_trace_enabled" in changed_fields:
        audit_bits.append(f"skip_trace_enabled={bool(config.skip_trace_enabled)}->{eff_skip_trace}")
    if "doc_types" in changed_fields:
        audit_bits.append(f"doc_types={config.doc_types}->{eff_doc_types}")
    if "include_living_owner_tod" in changed_fields:
        audit_bits.append(
            f"include_living_owner_tod={config.include_living_owner_tod}"
            f"->{eff_include_living_owner_tod}"
        )
    audit_detail = " ".join(audit_bits)

    # 9. Apply, flush, refresh (so the response carries the new updated_at token).
    config.name = eff_name
    config.fields = eff_fields
    config.enrichment = eff_enrichment
    config.schedule = eff_schedule
    config.deliver = eff_deliver
    config.skip_trace_enabled = eff_skip_trace
    config.doc_types = eff_doc_types
    config.include_living_owner_tod = eff_include_living_owner_tod
    await db.flush()
    await db.refresh(config)

    audit_log(request, "scraper_updated", current_user.id, audit_detail)
    return ScraperConfigResponse.model_validate(config)


# ─── Admin: County connector management ──────────────────────────────────────


@router.post(
    "/connectors",
    response_model=ConnectorResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin_mfa)],
)
async def create_connector(
    body: ConnectorCreate,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ConnectorResponse:
    """Add a new county connector. Agency plan only.

    Access (H2-P5): require_admin_mfa gates this route. Creating a connector is a
    state-changing admin op (it registers a new SSRF-allowlisted scrape target),
    so it demands a STEP-UP session — admin + enrolled MFA + a fresh, MFA-backed
    JWT (not an API key). Non-admins get 404; un-enrolled admins get 403
    admin_mfa_enrollment_required; a stale or non-MFA session gets 403
    admin_mfa_step_up_required. The inline is_admin check is gone — central
    dependency so the gate can't drift per-endpoint (Codex HIGH).

    For AI-mode connectors, no per-county Python scraper code is needed — the
    registry detects the recorder-platform template from the portal URL. The URL
    must match a known platform (rejected with 400 otherwise).
    """
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

    # This endpoint only creates AI/template-mode connectors — it has no
    # scraper_class input, so it cannot configure a manual (code-backed) scraper.
    # Manual connectors are provisioned via migrations/seeds with an allowlisted
    # scraper_class. Reject manual mode here rather than persist an empty
    # scraper_class that would crash get_scraper_class() at scrape time.
    if body.scraper_mode != "ai":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Only AI-mode (template-detected) connectors can be created via the API. "
                "Manual-mode connectors require a code-backed scraper class and are "
                "provisioned via a migration."
            ),
        )

    # AI-mode connectors resolve to a recorder-platform TEMPLATE by base_url
    # (the generic AI scraper was removed). Reject at creation if the URL matches
    # no known template — otherwise the connector would fail at scrape time with
    # UnsupportedCountyError. scraper_class stays empty: ai-mode resolution uses
    # base_url (the ai branch returns before any scraper_class import).
    from src.scrapers.registry import has_template
    if not has_template(body.base_url):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "No scraper template matches this portal URL. AI-mode connectors "
                "require a recognized recorder platform (EagleWeb, AcclaimWeb, Tyler "
                "SelfService, LandmarkWeb, AVA Fidlar, Laserfiche, Skagit, iDocMarket)."
            ),
        )

    connector = CountyConnector(
        id=str(uuid.uuid4()),
        county=body.county,
        state=body.state.upper(),
        record_types=body.record_types,
        scraper_class="",
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

# Older cache writers stored this literal when a parcel lookup came back empty.
# It is a placeholder, not an address — the API must return null for it so the
# UI renders "—" rather than a fake value (the same rule the results page has).
_CACHE_ADDRESS_PLACEHOLDER = "(enrichment unavailable)"


def _cache_address_or_none(value: str | None) -> str | None:
    if not value or value.strip() == _CACHE_ADDRESS_PLACEHOLDER:
        return None
    return value


def _cached_doc_type_filter(record_type: str) -> tuple[str | None, dict]:
    r"""SQL fragment + bind params restricting county_records to one record type.

    A typed config gets ONLY rows whose doc_type matches its keywords. The former
    `doc_type IS NULL OR …` escape hatch let every untyped row through to every
    record type: on 2026-09-02 the entire cache (3,305 rows, all doc_type NULL,
    scraped in March by a since-replaced parser) was served under a probate
    config as if it were probate — column-shifted rows, NULL party names and the
    literal "(enrichment unavailable)" included. Keywords are hardcoded values
    (never user input) bound as parameters; the fragment is never f-string
    interpolated with them. (None, {}) for a record type with no keyword list.

    Mirrors the scraper's own matcher (eagleweb._doc_type_matches +
    _DOC_TYPE_EXCLUDE, Codex review): multi-word / long keywords are substring
    matches (ILIKE), short single-token codes (<=5 chars: SUCC, NTS, TOD, …) are
    WORD-BOUNDARY matches (PostgreSQL ARE `\m…\M`) so "SUCC" cannot bleed into
    "SUCCESSOR TRUSTEE", and the record type's exclude phrases drop the row.
    """
    from src.scrapers.templates.eagleweb import _DOC_TYPE_EXCLUDE, _DOC_TYPE_MAP

    keywords = _DOC_TYPE_MAP.get(record_type, [])
    if not keywords:
        return None, {}
    conditions = []
    params: dict = {}
    for i, kw in enumerate(keywords):
        name = f"kw_{i}"
        if " " in kw or len(kw) > 5:
            conditions.append(f"doc_type ILIKE :{name}")
            params[name] = f"%{kw}%"
        else:
            conditions.append(f"doc_type ~* :{name}")
            params[name] = rf"\m{re.escape(kw)}\M"
    clause = "(" + " OR ".join(conditions) + ")"
    excludes = _DOC_TYPE_EXCLUDE.get(record_type, [])
    if excludes:
        ex_conditions = []
        for i, ex in enumerate(excludes):
            name = f"ex_{i}"
            ex_conditions.append(f"doc_type ILIKE :{name}")
            params[name] = f"%{ex}%"
        clause += " AND NOT (" + " OR ".join(ex_conditions) + ")"
    return clause, params


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
    type_clauses = []
    query_params: dict = {}
    doc_type_clause, doc_type_params = _cached_doc_type_filter(config.record_type)
    if doc_type_clause:
        type_clauses.append(doc_type_clause)
        query_params.update(doc_type_params)

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
                property_address=_cache_address_or_none(r.property_address),
                mailing_address=_cache_address_or_none(r.mailing_address),
                is_new=r.is_new,
                scraped_at=r.scraped_at,
            )
            for r in rows
        ],
    )


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
    are never re-pushed. The reset runs on the RLS app session (H1: the API holds
    app-role credentials only — no system session in the request path) with the
    explicit user_id filter kept as the suspenders.
    """
    from sqlalchemy import update

    from src.db.models import DialerDelivery, Job

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

    reset = (
        await db.execute(
            update(DialerDelivery)
            .where(
                DialerDelivery.job_id == job_id,
                DialerDelivery.user_id == current_user.id,
                DialerDelivery.status == "failed",
            )
            .values(status="pending", last_error=None)
        )
    ).rowcount
    await db.commit()
    reset = int(reset or 0)
    if reset:
        from src.workers.dialer_outbox import process_dialer_outbox
        process_dialer_outbox.delay(job_id)
    return {"job_id": job_id, "replayed": reset}
