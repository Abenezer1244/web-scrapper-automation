"""Database unreachable → 503, everything else stays 500.

Regression cover for the 2026-10-01 incident: the database stopped answering,
POST /auth/login returned the generic 500, and the app showed "Something went
wrong", the same as a bug. Nothing outside could tell an outage from a defect.

No mocks (per .claude/rules/testing.md). The outage cases send a real request
through the real app to a real engine whose database cannot be reached, so the
exception is the one asyncpg actually raises. The classifier cases use real
exception instances raised from real code paths.
"""

import asyncio
import re

import asyncpg.exceptions as pg_exc
import pytest
import pytest_asyncio
import sqlalchemy.exc as sa_exc
from httpx import ASGITransport, AsyncClient
from main import app
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import StreamingResponse
from starlette.routing import Route

from src.api.errors import is_database_unavailable
from src.api.middleware import DatabaseUnavailableMiddleware
from src.db import get_db

# 203.0.113.0/24 is TEST-NET-3 (RFC 5737): never routable, so the connect times out.
_UNROUTABLE_URL = "postgresql+asyncpg://nobody:nobody@203.0.113.1:5432/nonexistent_test"
# Port 1 on loopback: nothing listens, so the connect is refused at once.
_REFUSED_URL = "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nonexistent_test"

_LOGIN = {"email": "outage-probe@example.com", "password": "not-a-real-password-1"}


@pytest_asyncio.fixture(params=[_UNROUTABLE_URL, _REFUSED_URL], ids=["unroutable", "refused"])
async def outage_client(request):
    """The real app, with get_db bound to a database that cannot be reached.

    The transport keeps raise_app_exceptions=True on purpose: the middleware
    must ANSWER the outage. Were it left to the catch-all handler, Starlette
    would re-raise into the test and fail it.
    """
    engine = create_async_engine(
        request.param,
        poolclass=NullPool,
        connect_args={"statement_cache_size": 0, "timeout": 2},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def _unreachable_db():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = _unreachable_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()


# ─── Through the app ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_login_returns_503_when_database_unreachable(outage_client: AsyncClient):
    res = await outage_client.post("/auth/login", json=_LOGIN)

    assert res.status_code == 503
    assert res.headers["retry-after"] == "30"
    assert res.headers["cache-control"] == "no-store"
    body = res.json()
    assert set(body) == {"detail", "ref"}
    assert body["detail"] == "Service temporarily unavailable"
    assert re.fullmatch(r"[0-9a-f]{12}", body["ref"])


@pytest.mark.asyncio
async def test_503_is_readable_cross_origin(outage_client: AsyncClient):
    """The app calls this API from the browser, cross-origin. A 503 without CORS
    headers reaches its fetch as an opaque network error, and the login page
    could only ever say "Something went wrong"."""
    origin = "https://app.bridgeleads.io"
    res = await outage_client.post("/auth/login", json=_LOGIN, headers={"Origin": origin})

    assert res.status_code == 503
    assert res.headers["access-control-allow-origin"] == origin
    assert "retry-after" in res.headers["access-control-expose-headers"].lower()
    # It went back out through SecurityHeadersMiddleware too.
    assert res.headers["x-content-type-options"] == "nosniff"


# ─── The middleware, on a minimal real ASGI app ──────────────────────────────
# Routes the product does not have: an app-code wrapper around a database error,
# an ordinary bug, and a failure after streaming has begun.

def _wrapped_outage():
    try:
        raise pg_exc.ConnectionFailureError("authentication did not complete within 15000ms")
    except pg_exc.ConnectionFailureError as exc:
        raise RuntimeError("could not load the account") from exc


async def _failing_stream():
    yield b"first chunk"
    raise pg_exc.ConnectionFailureError("connection lost mid-stream")


def _small_app() -> Starlette:
    async def wrapped(request):
        _wrapped_outage()

    async def bug(request):
        raise ValueError("an ordinary bug")

    async def streamed(request):
        return StreamingResponse(_failing_stream())

    small = Starlette(routes=[
        Route("/wrapped", wrapped), Route("/bug", bug), Route("/streamed", streamed),
    ])
    small.add_middleware(DatabaseUnavailableMiddleware)
    small.add_middleware(CORSMiddleware, allow_origins=["https://app.example"])
    return small


@pytest.mark.asyncio
async def test_wrapped_database_error_still_gets_a_readable_503():
    async with AsyncClient(transport=ASGITransport(app=_small_app()), base_url="http://t") as c:
        res = await c.get("/wrapped", headers={"Origin": "https://app.example"})

    assert res.status_code == 503
    assert res.headers["access-control-allow-origin"] == "https://app.example"


@pytest.mark.asyncio
async def test_other_exceptions_pass_through_untouched():
    async with AsyncClient(transport=ASGITransport(app=_small_app()), base_url="http://t") as c:
        with pytest.raises(ValueError, match="an ordinary bug"):
            await c.get("/bug")


@pytest.mark.asyncio
async def test_failure_after_response_started_is_reraised():
    """A second response cannot be sent once the first has started."""
    async with AsyncClient(transport=ASGITransport(app=_small_app()), base_url="http://t") as c:
        with pytest.raises(pg_exc.ConnectionFailureError):
            await c.get("/streamed")


@pytest.mark.asyncio
async def test_503_body_leaks_no_database_internals(outage_client: AsyncClient):
    res = await outage_client.post("/auth/login", json=_LOGIN)

    text = res.text.lower()
    for leak in ("asyncpg", "sqlalchemy", "203.0.113", "127.0.0.1", "nobody", "traceback"):
        assert leak not in text


# ─── The classifier ───────────────────────────────────────────────────────────

def _raise(exc: BaseException) -> BaseException:
    try:
        raise exc
    except BaseException as caught:  # noqa: BLE001 — returning it for inspection
        return caught


def _dbapi_error(orig: Exception, *, invalidated: bool) -> sa_exc.DBAPIError:
    return sa_exc.DBAPIError.instance(
        "SELECT 1", {}, orig, Exception, connection_invalidated=invalidated
    )


def test_server_side_connection_failure_is_unavailable():
    # The exact error of 2026-10-01, sqlstate 08006.
    exc = _raise(pg_exc.ConnectionFailureError("authentication did not complete within 15000ms"))
    assert is_database_unavailable(exc)


@pytest.mark.parametrize(
    "cls", [pg_exc.CannotConnectNowError, pg_exc.TooManyConnectionsError]
)
def test_server_refusing_connections_is_unavailable(cls):
    assert is_database_unavailable(_raise(cls("refusing connections")))


def test_pool_checkout_timeout_is_unavailable():
    assert is_database_unavailable(_raise(sa_exc.TimeoutError("QueuePool limit reached")))


def test_connection_lost_under_a_statement_is_unavailable():
    exc = _dbapi_error(pg_exc.ConnectionDoesNotExistError("closed mid-operation"), invalidated=True)
    assert is_database_unavailable(exc)


def test_unavailable_cause_found_through_the_chain():
    try:
        try:
            raise pg_exc.ConnectionFailureError("auth timeout")
        except pg_exc.ConnectionFailureError as inner:
            raise RuntimeError("wrapped by app code") from inner
    except RuntimeError as outer:
        assert is_database_unavailable(outer)


def test_unavailable_context_found_when_cause_points_elsewhere():
    """`raise X from Y` inside an except block: the database error is only on
    __context__, and __cause__ is an unrelated exception."""
    try:
        try:
            raise pg_exc.ConnectionFailureError("auth timeout")
        except pg_exc.ConnectionFailureError:
            raise RuntimeError("cleanup failed") from ValueError("unrelated")
    except RuntimeError as outer:
        assert is_database_unavailable(outer)


def test_unavailable_inside_a_task_group_is_found():
    """A TaskGroup raises its children's failures as one ExceptionGroup."""

    async def _query():
        raise pg_exc.ConnectionFailureError("auth timeout")

    async def _gather():
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_query())

    try:
        asyncio.run(_gather())
    except ExceptionGroup as group:
        assert is_database_unavailable(group)
    else:
        pytest.fail("TaskGroup did not raise")


def test_statement_timeout_stays_500():
    exc = _dbapi_error(pg_exc.QueryCanceledError("canceling statement due to statement timeout"),
                       invalidated=False)
    assert not is_database_unavailable(exc)


def test_supavisor_tenant_not_found_stays_500():
    # 2026-07-28: a paused project. Retrying in 30 s does not fix that.
    exc = _raise(pg_exc.InternalServerError("(ENOTFOUND) tenant/user x.y not found"))
    assert not is_database_unavailable(exc)


def test_app_level_timeout_stays_500():
    """A TimeoutError raised outside asyncpg (an outbound call, an app deadline)
    is not evidence about the database."""

    async def _deadline():
        async with asyncio.timeout(0.01):
            await asyncio.sleep(1)

    try:
        asyncio.run(_deadline())
    except TimeoutError as exc:
        assert not is_database_unavailable(exc)
    else:
        pytest.fail("asyncio.timeout did not fire")


def test_bare_connection_error_outside_asyncpg_stays_500():
    assert not is_database_unavailable(_raise(ConnectionRefusedError("outbound HTTP refused")))


@pytest.mark.parametrize("exc", [ValueError("bug"), KeyError("bug"), asyncio.CancelledError()])
def test_ordinary_exceptions_stay_500(exc):
    assert not is_database_unavailable(_raise(exc))


def test_cyclic_exception_chain_terminates():
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__context__, b.__context__ = b, a
    assert not is_database_unavailable(a)
