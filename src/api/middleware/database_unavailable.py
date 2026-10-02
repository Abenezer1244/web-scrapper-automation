"""Database unreachable → 503 the browser can read.

The catch-all ``Exception`` handler in main.py cannot do this. Starlette runs
that handler in ServerErrorMiddleware, OUTSIDE every app middleware, so its
response carries neither CORS nor security headers. The app calls this API from
the browser, cross-origin, and a response without CORS headers reaches its
fetch as an opaque network error: the login page could only ever say
"Something went wrong".

This middleware is registered innermost (first ``add_middleware`` in main.py),
so its 503 travels back out through SecurityHeadersMiddleware and CORSMiddleware
like any normal response. It catches an exception only when
``is_database_unavailable()`` says the database could not be reached, wrapped
or not; everything else is re-raised untouched to the catch-all, which still
answers 500. Once the response has started (a stream, a background task) it
re-raises too: a second response cannot be sent.
"""

from __future__ import annotations

import logging
import uuid

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.api.errors import is_database_unavailable

_logger = logging.getLogger("api.unhandled")

# How long a client should wait before retrying. A floor, not a schedule: the
# app backs off from it.
RETRY_AFTER_SECONDS = 30


class DatabaseUnavailableMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def _send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception as exc:
            if started or not is_database_unavailable(exc):
                raise
            ref = uuid.uuid4().hex[:12]
            _logger.error(
                "Database unavailable ref=%s method=%s path=%s",
                ref, scope.get("method"), scope.get("path"), exc_info=exc,
            )
            # no-store: no cache may replay this after the database is back.
            response = JSONResponse(
                status_code=503,
                content={"detail": "Service temporarily unavailable", "ref": ref},
                headers={"Retry-After": str(RETRY_AFTER_SECONDS), "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
