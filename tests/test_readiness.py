"""Readiness probe tests — /health vs /ready.

Regression cover for the 2026-07-28 incident: the Supabase project disappeared,
every DB-backed route returned 500, and /health still answered 200 so nothing
alerted. /ready exists to make that state observable.

No mocks (per .claude/rules/testing.md). The "database is down" cases use a REAL
engine pointed at an unroutable address, so the failure is a genuine connection
error travelling the real code path — not a simulated one.
"""

import asyncio

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import src.api.readiness as readiness
import src.db.session as _db_session

# 203.0.113.0/24 is TEST-NET-3 (RFC 5737) — reserved for documentation and
# guaranteed never routable, so this fails to connect without depending on a
# firewall rule or DNS behaviour that could differ between machines.
_DEAD_DB_URL = (
    "postgresql+asyncpg://nobody:nobody@203.0.113.1:5432/nonexistent"
)


@pytest.fixture(autouse=True)
def _clear_readiness_cache():
    """Each test starts with no cached probe result and ends leaving none.

    The cache is process-global by design, so without this a result from one
    test would leak into the next and make ordering matter.
    """
    readiness._cached = None
    yield
    readiness._cached = None


@pytest_asyncio.fixture
async def dead_engine():
    """A real engine whose host cannot be reached. Disposed after the test."""
    engine = create_async_engine(
        _DEAD_DB_URL,
        poolclass=NullPool,
        connect_args={"statement_cache_size": 0},
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def swap_engine(monkeypatch):
    """Point the readiness probe at a different real engine.

    monkeypatch (not assignment) so the module attribute is always restored even
    if the test fails partway.
    """

    def _swap(engine):
        monkeypatch.setattr(_db_session, "async_engine", engine)

    return _swap


# ─── Happy path ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ready_returns_200_when_database_reachable(client: AsyncClient):
    res = await client.get("/ready")

    assert res.status_code == 200
    assert res.json() == {"status": "ready"}


@pytest.mark.asyncio
async def test_health_stays_static_and_dependency_free(client: AsyncClient):
    """/health must remain pure liveness — no DB work, no failure mode.

    This is what makes it safe to wire to a platform health gate: it cannot be
    held down by a downstream outage, so a fix can still be deployed mid-incident.
    """
    res = await client.get("/health")

    assert res.status_code == 200
    assert res.json() == {"status": "ok", "service": "bridgeleads-api"}


# ─── Outage path ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ready_returns_503_when_database_unreachable(
    client: AsyncClient, dead_engine, swap_engine
):
    swap_engine(dead_engine)

    res = await client.get("/ready")

    assert res.status_code == 503
    body = res.json()
    assert body["status"] == "degraded"
    assert body["ref"]  # correlates the response with the logged traceback


@pytest.mark.asyncio
async def test_ready_503_body_leaks_no_database_internals(
    client: AsyncClient, dead_engine, swap_engine
):
    """The 503 is unauthenticated — it must not describe internal topology.

    Project rule: errors carry a reference id, never a stack trace or raw DB
    error (.claude/rules/security.md).
    """
    swap_engine(dead_engine)

    res = await client.get("/ready")

    assert set(res.json()) == {"status", "ref"}
    leaked = res.text.lower()
    for term in (
        "asyncpg", "postgres", "traceback", "203.0.113.1",
        "password", "nobody", "connect", "sqlalchemy",
    ):
        assert term not in leaked, f"/ready 503 body leaked {term!r}"


@pytest.mark.asyncio
async def test_health_still_200_while_ready_is_503(
    client: AsyncClient, dead_engine, swap_engine
):
    """The exact split the incident needed: liveness green, readiness red."""
    swap_engine(dead_engine)

    assert (await client.get("/ready")).status_code == 503
    assert (await client.get("/health")).status_code == 200


# ─── Caching and single-flight ────────────────────────────────────────────────
# These are the properties that stop an unauthenticated /ready from becoming a
# connection-exhaustion amplifier against Supabase (the async engine is
# NullPool, so every uncached probe is a brand-new TCP+TLS+auth connection).

@pytest.mark.asyncio
async def test_result_is_reused_within_ttl(
    client: AsyncClient, dead_engine, swap_engine
):
    """A cached result is served without touching the database again.

    Proven by swapping in a dead engine AFTER a successful probe: if the second
    call hit the database it would fail, so a 200 can only come from the cache.
    """
    assert (await client.get("/ready")).status_code == 200

    swap_engine(dead_engine)

    assert (await client.get("/ready")).status_code == 200


@pytest.mark.asyncio
async def test_expired_cache_is_refreshed(
    client: AsyncClient, dead_engine, swap_engine, monkeypatch
):
    """Once the TTL lapses the next call probes again and sees the new state."""
    assert (await client.get("/ready")).status_code == 200

    swap_engine(dead_engine)
    monkeypatch.setattr(readiness, "_TTL_SECONDS", 0.0)  # expire immediately

    assert (await client.get("/ready")).status_code == 503


@pytest.mark.asyncio
async def test_concurrent_probes_open_only_one_connection(client: AsyncClient):
    """A stampede of concurrent callers must share ONE database connection.

    Without the single-flight lock, every caller misses the cold cache at the
    same moment and opens its own connection — which is precisely the
    amplification an unauthenticated endpoint must not offer.
    """
    connections = 0

    @event.listens_for(_db_session.async_engine.sync_engine, "connect")
    def _count(dbapi_conn, record):
        nonlocal connections
        connections += 1

    try:
        results = await asyncio.gather(
            *(client.get("/ready") for _ in range(12))
        )
    finally:
        event.remove(_db_session.async_engine.sync_engine, "connect", _count)

    assert all(r.status_code == 200 for r in results)
    assert connections == 1, (
        f"expected a single shared probe, got {connections} connections"
    )
