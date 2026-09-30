"""The `lookup_quote` rate-limit zone (contact lookup 1b-1c, precursor to the quote).

The quote (POST /jobs/{id}/contact-lookups/quote) walks up to 20,000 of a tab's leads
and writes a Redis key per call. It gets its OWN bucket: sharing `export` (every
full-CSV download since audit #4 S4-03) would let quoting spend the customer's
download budget, and downloads block quoting. These tests drive the real limiter
against real Redis; the route itself lands in the next PR.
"""
from __future__ import annotations

import importlib
import time
import types
import uuid

import pytest
import redis.asyncio as aioredis
from fastapi import HTTPException
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from starlette.requests import Request

# The package re-exports the rate_limit FUNCTION under the module name.
rl = importlib.import_module("src.api.middleware.rate_limit")

# Literal on purpose: a change of the budget must be a deliberate test edit.
QUOTE_BUDGET = 10
EXPORT_BUDGET = 20


@pytest.fixture(autouse=True)
def _limiter_clock(monkeypatch):
    """The limiter's clock only, 1 ms per request from the real time at test start,
    so every request of a test falls inside one 60 s window (the export-zone tests'
    pattern). Redis and the fallback still run for real."""
    start, ticks = time.time(), iter(range(10**9))
    monkeypatch.setattr(rl, "time", types.SimpleNamespace(time=lambda: start + next(ticks) / 1000))


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [],
                    "client": ("127.0.0.1", 50000)})


async def _allowed(zone: str, user: str) -> bool:
    try:
        await rl.rate_limit(_request(), zone=zone, identifier=user)
    except HTTPException as exc:
        assert exc.status_code == 429
        return False
    return True


def test_the_zone_is_ten_a_minute_and_fails_closed():
    assert rl._ZONES["lookup_quote"] == (QUOTE_BUDGET, 60)
    assert "lookup_quote" in rl._FALLBACK_ZONES


async def test_ten_quotes_a_minute_then_429():
    user = str(uuid.uuid4())
    for i in range(QUOTE_BUDGET):
        assert await _allowed("lookup_quote", user), f"quote {i + 1} refused"
    assert not await _allowed("lookup_quote", user)


async def test_quoting_never_spends_the_download_budget():
    user = str(uuid.uuid4())
    for _ in range(QUOTE_BUDGET):
        assert await _allowed("lookup_quote", user)
    assert not await _allowed("lookup_quote", user)
    for i in range(EXPORT_BUDGET):
        assert await _allowed("export", user), f"export {i + 1} refused after quoting"


async def test_downloads_never_spend_the_quote_budget():
    user = str(uuid.uuid4())
    for _ in range(EXPORT_BUDGET):
        assert await _allowed("export", user)
    assert not await _allowed("export", user)
    assert await _allowed("lookup_quote", user)


async def test_each_account_has_its_own_bucket():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    for _ in range(QUOTE_BUDGET):
        assert await _allowed("lookup_quote", a)
    assert not await _allowed("lookup_quote", a)
    assert await _allowed("lookup_quote", b)


async def test_redis_down_still_limits_quotes(monkeypatch):
    """In `_FALLBACK_ZONES`: an outage keeps a per-process limit instead of opening
    unlimited tab scans."""
    dead = aioredis.from_url(
        "redis://127.0.0.1:1/0", socket_connect_timeout=0.05, socket_timeout=0.05,
        retry=Retry(NoBackoff(), 0), retry_on_error=[],
    )
    monkeypatch.setattr(rl, "_redis_client", dead)
    monkeypatch.setattr(rl, "_fallback_hits", {})
    user = str(uuid.uuid4())
    try:
        for i in range(QUOTE_BUDGET):
            assert await _allowed("lookup_quote", user), f"quote {i + 1} refused"
        assert not await _allowed("lookup_quote", user)
    finally:
        await dead.aclose()
