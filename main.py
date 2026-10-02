import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api import (
    analytics_router,
    auth_router,
    batches_router,
    billing_router,
    jobs_router,
    notifications_router,
    scrapers_router,
    segments_router,
    webhooks_router,
)
from src.api.errors import RunRefusedHTTPException, run_refused_handler
from src.api.middleware import DatabaseUnavailableMiddleware, SecurityHeadersMiddleware
from src.api.readiness import database_ready
from src.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    # Load every active county connector's domain into the SSRF
    # allowlist. Connectors seeded via Alembic migration never pass
    # through the API route that calls validate_scraping_target(), so
    # without this call the scrape worker would reject them with
    # "Scraping target not in approved domain list". See Sprint 6.3
    # Phase 3 audit in docs/compliance/connector-audit-2026-04-10.md
    from src.api.middleware import register_connector_domains_from_db
    register_connector_domains_from_db()
    # FAIL-CLOSED boot gate (not merely advisory, as this comment used to
    # say): with RLS_ENFORCE on — which production sets on both api and
    # worker — this REFUSES TO START if the DB role bypasses RLS, because
    # a bypassing role makes all 47 policies inert and leaves tenant
    # isolation to the application WHERE filters alone. With RLS_ENFORCE
    # off it only logs. C2 from the full-SaaS code review — see
    # docs/compliance/connector-audit-2026-04-10.md follow-ups.
    from src.db.session import check_rls_role_status
    check_rls_role_status()
    # Fail fast at boot if field encryption is misconfigured (production/strict with
    # no FIELD_ENCRYPTION_KEY): build the Fernet now so a bad crypto config breaks
    # startup rather than the first PII operation. _build_fernet() refuses the
    # SECRET_KEY-derived fallback in prod/strict (incident 2026-06).
    from src.utils.crypto import _instance
    _instance()
    yield


app = FastAPI(
    title="BridgeLeads API",
    version="1.0.0",
    # Docs only available in debug/development mode
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
    lifespan=lifespan,
)

# ─── Middleware ────────────────────────────────────────────────────────────────

# Added first, so it sits innermost: its 503 then gets the security and CORS
# headers below, which the catch-all Exception handler's responses never do.
app.add_middleware(DatabaseUnavailableMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://bridgeleads.io",
        "https://app.bridgeleads.io",
        "https://bridgeleads-web.vercel.app",
        *settings.get_allowed_origins(),
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Requested-With"],
    # Retry-After is not CORS-safelisted, so without this the app (a different
    # origin from the API) cannot read how long to wait after a 429/503.
    expose_headers=["Retry-After"],
)

# ─── Routers ──────────────────────────────────────────────────────────────────

app.include_router(auth_router)
app.include_router(scrapers_router)
app.include_router(jobs_router)
app.include_router(billing_router)
app.include_router(webhooks_router)
app.include_router(segments_router)
app.include_router(batches_router)
app.include_router(notifications_router)
app.include_router(analytics_router)


# ─── Global exception handler ─────────────────────────────────────────────────
# Any uncaught exception returns a generic message + a reference id (logged
# server-side), so a stack trace is never sent to the client even if DEBUG is
# accidentally enabled. HTTPException / validation errors keep their own
# handlers (this only catches the otherwise-unhandled).
_unhandled_logger = logging.getLogger("api.unhandled")


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    ref = uuid.uuid4().hex[:12]
    _unhandled_logger.exception(
        "Unhandled error ref=%s method=%s path=%s", ref, request.method, request.url.path
    )
    return JSONResponse(status_code=500, content={"detail": "Internal error", "ref": ref})


# A refused run's 402 carries a machine code beside its unchanged sentence
# (src/api/errors.py). Resolved by MRO, so it wins over the HTTPException default.
app.add_exception_handler(RunRefusedHTTPException, run_refused_handler)


# ─── Logging: strip secrets from access logs ─────────────────────────────────

import re  # noqa: E402 — section-local; kept beside the filter it exists for

_TOKEN_RE = re.compile(r"token=[A-Za-z0-9_\-\.]+")

# The legacy `POST /webhooks/tracerfy/{provided_secret}` route carries the shared
# secret in the URL PATH, so uvicorn's access line logs a live credential in
# cleartext on every delivery — and Tracerfy currently sends no header, so that is
# the route in active use. `_TOKEN_RE` does not cover it (it only matches a
# `token=` query param) and logger.py has no URL-path rule.
#
# MITIGATION, NOT REMEDIATION. This stops new access-log lines from carrying the
# secret. It does NOT undo the exposure: the secret is already in historical logs,
# and any proxy in front of the app (Railway's edge) logs the URL independently of
# this filter. The fix is to migrate Tracerfy to the `X-Tracerfy-Webhook-Secret`
# header (already authoritative when present), rotate TRACERFY_WEBHOOK_SECRET, then
# delete the legacy route in src/api/routes/webhooks.py.
_PATH_SECRET_RE = re.compile(r"(/webhooks/tracerfy/)[^/\s?\"']+")

_ACCESS_LOG_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_TOKEN_RE, "token=REDACTED"),
    (_PATH_SECRET_RE, r"\1[REDACTED]"),
)


def _scrub_access_log_value(value: str) -> str:
    for pattern, replacement in _ACCESS_LOG_REDACTIONS:
        value = pattern.sub(replacement, value)
    return value


class _StripTokenFilter(logging.Filter):
    """Redact download tokens and path-borne secrets from uvicorn access lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        if hasattr(record, "args") and record.args:
            record.args = tuple(
                _scrub_access_log_value(str(a)) if isinstance(a, str) else a
                for a in record.args
            )
        return True


logging.getLogger("uvicorn.access").addFilter(_StripTokenFilter())

# PII/secret redaction backstop for loggers created via logging.getLogger()
# (middleware, etc.) that bypass setup_logger()'s per-handler filter.
from src.utils.logger import install_global_redaction  # noqa: E402,I001 — must run AFTER the handlers above are attached

install_global_redaction()


# ─── Health / readiness ───────────────────────────────────────────────────────
# Two endpoints answering two different questions — see src/api/readiness.py.
#
#   /health  LIVENESS  — the process is up. Touches nothing downstream, so a
#                        platform health gate wired here is never held down by a
#                        dependency outage and you can still deploy mid-incident.
#   /ready   READINESS — a real database round-trip. This is the one to alert on;
#                        /health returning 200 during a total DB outage is what
#                        let the 2026-07-28 Supabase incident go unnoticed.

@app.get("/health", tags=["system"])
async def health() -> dict:
    return {"status": "ok", "service": "bridgeleads-api"}


@app.get(
    "/ready",
    tags=["system"],
    # 503 is a NORMAL outcome here, not an exception path, so it belongs in the
    # published contract — schema/openapi.json generates the frontend types and
    # is what a monitor would be built against. Codex review [P3].
    responses={
        200: {
            "description": "Database reachable; this instance can serve traffic.",
            "content": {
                "application/json": {"example": {"status": "ready"}}
            },
        },
        503: {
            "description": (
                "A required dependency is unreachable. The failing dependency is "
                "named only in the server logs, correlated by `ref`."
            ),
            "content": {
                "application/json": {
                    "example": {"status": "degraded", "ref": "0d39ea7a3c47"}
                }
            },
        },
    },
)
async def ready() -> JSONResponse:
    """503 when the database is unreachable, 200 otherwise.

    The body stays coarse on purpose. This endpoint is unauthenticated, and
    naming which dependency is down hands an attacker a free map of internal
    topology for no operational gain — the ref ties the response to the full
    traceback in the logs, which is where responders actually look.
    """
    is_ready, ref = await database_ready()
    if is_ready:
        return JSONResponse(status_code=200, content={"status": "ready"})
    return JSONResponse(
        status_code=503, content={"status": "degraded", "ref": ref}
    )
