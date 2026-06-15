import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api import (
    auth_router,
    batches_router,
    billing_router,
    jobs_router,
    scrapers_router,
    segments_router,
    webhooks_router,
)
from src.api.middleware import SecurityHeadersMiddleware
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
    # Advisory check: report whether the DB role bypasses RLS. If it
    # does, tenant isolation relies entirely on the application-level
    # WHERE filters. C2 from the full-SaaS code review — see
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
)

# ─── Routers ──────────────────────────────────────────────────────────────────

app.include_router(auth_router)
app.include_router(scrapers_router)
app.include_router(jobs_router)
app.include_router(billing_router)
app.include_router(webhooks_router)
app.include_router(segments_router)
app.include_router(batches_router)


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


# ─── Logging: strip tokens from access logs ──────────────────────────────────

import re

_TOKEN_RE = re.compile(r"token=[A-Za-z0-9_\-\.]+")


class _StripTokenFilter(logging.Filter):
    """Redact download tokens from uvicorn access log lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        if hasattr(record, "args") and record.args:
            record.args = tuple(
                _TOKEN_RE.sub("token=REDACTED", str(a)) if isinstance(a, str) else a
                for a in record.args
            )
        return True


logging.getLogger("uvicorn.access").addFilter(_StripTokenFilter())

# PII/secret redaction backstop for loggers created via logging.getLogger()
# (middleware, etc.) that bypass setup_logger()'s per-handler filter.
from src.utils.logger import install_global_redaction

install_global_redaction()


# ─── Health check ─────────────────────────────────────────────────────────────

@app.get("/health", tags=["system"])
async def health() -> dict:
    return {"status": "ok", "service": "bridgeleads-api"}
