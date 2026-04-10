import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api import (
    auth_router,
    billing_router,
    jobs_router,
    scrapers_router,
    webhooks_router,
)
from src.api.middleware import SecurityHeadersMiddleware
from src.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
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


# ─── Health check ─────────────────────────────────────────────────────────────

@app.get("/health", tags=["system"])
async def health() -> dict:
    return {"status": "ok", "service": "bridgeleads-api"}
