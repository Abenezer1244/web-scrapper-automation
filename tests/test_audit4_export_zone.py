"""Audit #4 S4-03 (2026-09-27): every full-CSV export shares the ``export`` zone.

Audit #3 (S3-09) put ``/jobs/{id}/download`` and ``/export-url`` in a 20/min
per-user ``export`` zone that keeps a per-process fallback while Redis is down.
Four more routes rebuild and decrypt a full lead CSV and were left in
``general`` (60/min, fully open when Redis is down): the two batch downloads and
the two segment exports. These tests pin all six to one budget.
"""
from __future__ import annotations

import importlib
import time
import types
import uuid

import pytest
import redis.asyncio as aioredis
from httpx import AsyncClient
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from sqlalchemy import update

from src.db.models import BatchRun, Job, Result, ScraperBatch, ScraperConfig, User

# The package re-exports the rate_limit FUNCTION under the module name, so a plain
# `import ... as` would bind the function, not the module.
rl = importlib.import_module("src.api.middleware.rate_limit")


@pytest.fixture(autouse=True)
def _limiter_clock(monkeypatch):
    """The limiter's clock, and only the limiter's: it advances 1 ms per request
    from the real time at test start, so 21 requests always fall inside one 60 s
    window however slowly this machine builds the CSVs. A wall clock made these
    tests flaky (a Redis-down run took 57-70 s and the window slid past its first
    requests). The limiter, Redis and the fallback still run for real; this module
    is the only reader of the replaced name (`rate_limit.py`, `time.time()`)."""
    start, ticks = time.time(), iter(range(10**9))
    monkeypatch.setattr(rl, "time", types.SimpleNamespace(time=lambda: start + next(ticks) / 1000))

# Literal on purpose: on the unfixed code these tests must fail by getting no 429,
# not by a missing constant.
EXPORT_BUDGET = 20  # per user per minute, across every export route

_PW = "SecurePass1!"
_TYPES = ["probate", "pre_foreclosure"]

# The four routes S4-03 moved. `{job}`, `{batch}`, `{run}` are filled per account.
_NEW_EXPORTS = [
    ("GET", "/batches/{batch}/download", None),
    ("GET", "/batches/{batch}/runs/{run}/download", None),
    ("POST", "/segments/intersection/export", {"record_types": _TYPES}),
    ("POST", "/segments/union/export", {"record_types": _TYPES}),
]
_JOB_DOWNLOAD = ("GET", "/jobs/{job}/download", None)


async def _account(client: AsyncClient, db) -> dict:
    """A real registered session on a Business plan (segments are Business and
    above, and that gate runs before the limiter), owning one delivered job with
    one lead and one finished batch run over that job."""
    email = f"s403_{uuid.uuid4().hex[:8]}@test.bridgeleads.io"
    reg = await client.post("/auth/register", json={
        "first_name": "Ex", "last_name": "Zone", "email": email, "password": _PW,
    })
    assert reg.status_code == 201, reg.text
    auth = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    me = await client.get("/auth/me", headers=auth)
    assert me.status_code == 200, me.text
    user_id = me.json()["id"]
    await db.execute(update(User).where(User.id == user_id).values(plan="business"))

    config = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user_id, name="Pierce probate", county="pierce",
        state="WA", record_type="probate", fields=["party_name", "parcel_id"],
        enrichment=[], schedule={"frequency": "manual"}, deliver={"format": "csv", "emails": []},
    )
    db.add(config)
    await db.flush()
    job = Job(
        id=str(uuid.uuid4()), user_id=user_id, scraper_config_id=config.id,
        status="done", trigger="manual", export_key=f"exports/{user_id}/run.csv",
    )
    db.add(job)
    await db.flush()
    db.add(Result(
        id=str(uuid.uuid4()), job_id=job.id, user_id=user_id,
        party_name="S403 OWNER", parcel_id="0123456789",
        property_address="1 Main St, Tacoma, WA 98402",
    ))
    batch = ScraperBatch(
        id=str(uuid.uuid4()), user_id=user_id, name="S403 batch", state="WA",
        fields=["party_name"], enrichment=[], schedule={}, deliver={"emails": []},
    )
    db.add(batch)
    await db.flush()
    run = BatchRun(
        id=str(uuid.uuid4()), batch_id=batch.id, user_id=user_id, status="done",
        child_job_ids=[job.id],
        combined_export_key=f"exports/{user_id}/batch/{uuid.uuid4()}/combined.csv",
    )
    db.add(run)
    await db.commit()
    return {"auth": auth, "user_id": user_id, "job": job.id, "batch": batch.id, "run": run.id}


async def _call(client: AsyncClient, acct: dict, route, **ids) -> int:
    method, path, body = route
    url = path.format(**{"job": acct["job"], "batch": acct["batch"], "run": acct["run"], **ids})
    resp = await client.request(method, url, headers=acct["auth"], json=body)
    return resp.status_code


def _route_id(route) -> str:
    return route[1]


# ─── Regression: each fails on the unfixed code ──────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("route", _NEW_EXPORTS, ids=_route_id)
async def test_each_export_route_allows_20_a_minute_then_429(client, db, route):
    acct = await _account(client, db)
    for i in range(EXPORT_BUDGET):
        code = await _call(client, acct, route)
        assert code == 200, f"request {i + 1} of {EXPORT_BUDGET} got {code}"
    assert await _call(client, acct, route) == 429


@pytest.mark.asyncio
@pytest.mark.parametrize("last", _NEW_EXPORTS, ids=_route_id)
async def test_job_batch_and_segment_exports_share_one_budget(client, db, last):
    acct = await _account(client, db)
    mixed = [_JOB_DOWNLOAD, *_NEW_EXPORTS]
    for i in range(EXPORT_BUDGET):
        route = mixed[i % len(mixed)]
        code = await _call(client, acct, route)
        assert code == 200, f"request {i + 1} ({route[1]}) got {code}"
    assert await _call(client, acct, last) == 429


@pytest.mark.asyncio
@pytest.mark.parametrize("route", _NEW_EXPORTS[:2], ids=_route_id)
@pytest.mark.parametrize("whose", ["missing", "another_tenants"])
async def test_a_batch_download_miss_still_spends_the_budget(client, db, route, whose):
    """The limiter runs before the owner lookup: probing ids is throttled too."""
    acct = await _account(client, db)
    if whose == "missing":
        ids = {"batch": str(uuid.uuid4()), "run": str(uuid.uuid4())}
    else:
        other = await _account(client, db)
        ids = {"batch": other["batch"], "run": other["run"]}
    for i in range(EXPORT_BUDGET):
        code = await _call(client, acct, route, **ids)
        assert code == 404, f"request {i + 1} got {code}"
    assert await _call(client, acct, route, **ids) == 429


@pytest.mark.asyncio
@pytest.mark.parametrize("route", _NEW_EXPORTS, ids=_route_id)
async def test_export_routes_stay_throttled_when_redis_is_down(client, db, monkeypatch, route):
    """A real client pointed at a closed port, not a stub. `general` fails fully
    open here; `export` falls back to a per-process limiter. Short timeouts and no
    retries only keep the run fast; the window itself is held by `_limiter_clock`."""
    acct = await _account(client, db)
    dead = aioredis.from_url(
        "redis://127.0.0.1:1/0", socket_connect_timeout=0.05, socket_timeout=0.05,
        retry=Retry(NoBackoff(), 0), retry_on_error=[],
    )
    monkeypatch.setattr(rl, "_redis_client", dead)
    monkeypatch.setattr(rl, "_fallback_hits", {})
    try:
        for i in range(EXPORT_BUDGET):
            code = await _call(client, acct, route)
            assert code == 200, f"request {i + 1} got {code}"
        assert await _call(client, acct, route) == 429
    finally:
        await dead.aclose()


# ─── Controls: pass before and after the fix ─────────────────────────────────

@pytest.mark.asyncio
async def test_a_job_export_is_two_tokens_so_ten_a_minute(client, db):
    """`/export-url` then the `/download` it points at: both spend the one bucket
    (the documented ceiling is 10 complete job exports a minute)."""
    acct = await _account(client, db)
    for i in range(EXPORT_BUDGET // 2):
        url = await client.get(f"/jobs/{acct['job']}/export-url", headers=acct["auth"])
        assert url.status_code == 200, f"export {i + 1}: export-url got {url.status_code}"
        dl = await client.get(url.json()["url"])
        assert dl.status_code == 200 and "S403 OWNER" in dl.text, f"export {i + 1}: {dl.status_code}"
    url = await client.get(f"/jobs/{acct['job']}/export-url", headers=acct["auth"])
    assert url.status_code == 429


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [
    ("POST", "/segments/union", {"record_types": _TYPES}),
    ("POST", "/segments/intersection", {"record_types": _TYPES}),
    ("GET", "/jobs/{job}/results", None),
], ids=_route_id)
async def test_views_are_not_in_the_export_budget(client, db, route):
    """The JSON views stay in `general` (their own limit is finding S4-07)."""
    acct = await _account(client, db)
    for i in range(EXPORT_BUDGET + 5):
        code = await _call(client, acct, route)
        assert code == 200, f"request {i + 1} got {code}"
