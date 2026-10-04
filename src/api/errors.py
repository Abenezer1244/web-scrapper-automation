"""The run-refusal 402: a machine-readable code beside the unchanged sentence.

``POST /jobs`` and ``POST /batches`` refuse a run for the account rule (frozen,
ended, over the record limit). ``detail`` stays the same
sentence it has always been, byte for byte, so a client that reads it as a
string keeps working; ``code`` and ``resumes_at`` are added BESIDE it, which
``HTTPException`` cannot do, hence the subclass and its handler.

The subclass matters twice: Starlette picks the handler by the exception's MRO,
so this one wins over FastAPI's HTTPException handler and the catch-all; and if
the handler were ever not registered, the exception still renders as today's
plain ``{"detail": "<sentence>"}``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

import asyncpg
import asyncpg.exceptions as _pg
import sqlalchemy.exc as _sa
from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse

# The codes a run refusal can carry: the evaluator's codes that end in a prose
# 402. run_in_flight (409) and not_entitled (structured 402) have their own
# bodies. RunRefusalResponse.code is tested equal to this.
RUN_REFUSAL_CODES = ("frozen", "ended", "over_limit")


class RunRefusedHTTPException(HTTPException):
    def __init__(
        self,
        code: str,
        message: str,
        resumes_at: datetime | None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=message, headers=headers
        )
        self.code = code
        self.resumes_at = resumes_at


def run_refusal_http(code: str | None, message: str, resumes_at: datetime | None) -> HTTPException:
    """The 402 for a refused run. A code outside RUN_REFUSAL_CODES (none can
    reach the callers today) keeps the plain sentence, so an unexpected code
    never becomes a machine code a client would act on."""
    if code in RUN_REFUSAL_CODES:
        return RunRefusedHTTPException(code, message, resumes_at)
    return HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=message)


async def run_refused_handler(request: Request, exc: RunRefusedHTTPException) -> JSONResponse:
    from src.api.schemas import RunRefusalResponse

    # Through the model, not a raw dict: JSONResponse cannot encode a datetime,
    # and this is the serializer GET /scrapers uses for the same resumes_at, so
    # the page and the 402 carry the identical string.
    body = RunRefusalResponse(detail=exc.detail, code=exc.code, resumes_at=exc.resumes_at)
    return JSONResponse(
        status_code=exc.status_code,
        content=body.model_dump(mode="json"),
        headers=exc.headers,
    )


# ─── no-store on a contact-bearing route's errors ────────────────────────────
# A route that returns lead contacts (phones, emails) or a download capability
# sets `Cache-Control: no-store` on its success response. FastAPI builds an
# HTTPException's response separately from any injected Response, so an error
# from the same route went out with no Cache-Control at all, and a cache could
# keep a stale 404 / 409 for a URL that later serves PII. Each such route wraps
# its WHOLE body in this, so every error it raises carries the header too.
# Out of reach by design: dependency / validation errors raised before the body
# runs, and the global 500 handler (its body is only `detail` + `ref`).


@contextmanager
def no_store_errors() -> Iterator[None]:
    """Re-raise any HTTPException from the block with ``Cache-Control: no-store``
    merged into its headers. Its other headers (Retry-After, WWW-Authenticate)
    stay, and a Cache-Control it already carries is kept as is. Any other
    exception propagates untouched.

    A COPY is raised, of the same class and attributes: some exceptions are
    module-level singletons (auth's ``_CREDENTIALS_EXCEPTION``, reachable from
    the run CSV's in-body bearer check), and stamping the header onto the shared
    instance would leak it onto every later raise of it, app-wide."""
    try:
        yield
    except HTTPException as exc:
        headers = dict(exc.headers or {})
        if any(name.lower() == "cache-control" for name in headers):
            raise
        headers["Cache-Control"] = "no-store"
        # Not copy.copy: it rebuilds through __init__ from exc.args, which are
        # empty for an HTTPException built with keyword arguments.
        stamped = type(exc).__new__(type(exc))
        stamped.__dict__.update(exc.__dict__)
        stamped.args = exc.args
        stamped.headers = headers
        raise stamped.with_traceback(exc.__traceback__) from exc


# ─── Database unreachable → 503 ──────────────────────────────────────────────
# 2026-10-01: the database stopped answering and every DB route, login included,
# returned the generic 500, which the app shows as "Something went wrong", the
# same as a bug. DatabaseUnavailableMiddleware (src/api/middleware/) asks this
# function whether an uncaught exception means the database could not be
# reached, and answers 503 + Retry-After when it does.
#
# Deliberately narrow; anything not matched stays a 500:
#   * QueryCanceledError (57014, statement timeout) is not here: a slow query is
#     as likely a bug as an outage.
#   * Supavisor's "tenant/user not found" (an InternalServerError, XX000) is not
#     here: a paused or misrouted project is not fixed by retrying in 30 s.
#   * CancelledError is control flow, never an availability signal.
# Connect failures are NOT wrapped by SQLAlchemy: a refused port or a connect
# timeout escapes as a bare ConnectionRefusedError / TimeoutError. Those builtins
# also come from outbound HTTP and app-level timeouts, so they count only when
# raised inside asyncpg.

_UNAVAILABLE = (
    _pg.PostgresConnectionError,   # class 08, e.g. 08006 "authentication did not complete"
    _pg.CannotConnectNowError,     # 57P03, server starting up or shutting down
    _pg.TooManyConnectionsError,   # 53300
    _sa.TimeoutError,              # pool checkout timed out
)
# realpath: a symlinked or junctioned site-packages must still match.
_ASYNCPG_ROOT = os.path.normcase(os.path.realpath(os.path.dirname(asyncpg.__file__))) + os.sep
# A cap on exceptions examined, beside the cycle check: real chains are a handful
# deep, and past the cap the answer is the safe default, a 500.
_MAX_CHAIN = 32


def _raised_in_asyncpg(exc: BaseException) -> bool:
    tb = exc.__traceback__
    while tb is not None:
        filename = os.path.normcase(os.path.realpath(tb.tb_frame.f_code.co_filename))
        if filename.startswith(_ASYNCPG_ROOT):
            return True
        tb = tb.tb_next
    return False


def is_database_unavailable(exc: BaseException) -> bool:
    """True when ``exc`` (or anything in its cause/context chain) means the
    database could not be reached, as opposed to a query that failed."""
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending and len(seen) < _MAX_CHAIN:
        link = pending.pop()
        if id(link) in seen:
            continue
        seen.add(id(link))
        if isinstance(link, _UNAVAILABLE):
            return True
        # SQLAlchemy sets this when the connection died under a statement.
        if isinstance(link, _sa.DBAPIError) and link.connection_invalidated:
            return True
        if isinstance(link, (ConnectionError, TimeoutError)) and _raised_in_asyncpg(link):
            return True
        # Both branches: `raise X from Y` inside an except block sets them apart.
        pending.extend(e for e in (link.__context__, link.__cause__) if e is not None)
        # A TaskGroup raises its children's failures as one ExceptionGroup.
        if isinstance(link, BaseExceptionGroup):
            pending.extend(link.exceptions)
    return False
