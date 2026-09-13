"""Live job log stream admission (src/api/sse_leases.py + GET /jobs/{id}/logs).

Regression for 2026-09-13: the old INCR/DECR counter decremented in the
stream's ``finally``, which a client disconnect cancelled, so every refresh,
route change or closed tab leaked a slot until the user saw "Too many
concurrent streams (max 5)" with a single tab open.

The disconnect tests run the app on a real uvicorn socket: ASGITransport does
not cancel the response task on client close the way a server does, so it
cannot reproduce the leak.
"""

import asyncio
import json
import time
import uuid

import httpx
import pytest
import redis as sync_redis
import uvicorn
from httpx import AsyncClient
from main import app
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

import src.db.session as _db_session
from src.api import sse_leases
from src.api.routes import jobs as jobs_routes
from src.config import settings
from src.db.models import Job, JobLog, User
from src.workers.tasks_helpers.status import _publish_log

CAP = settings.SSE_MAX_STREAMS_PER_USER


def _leases_key(user_id: str) -> str:
    return f"sse_leases:{user_id}"


async def _set_job_status(job_id: str, new_status: str) -> None:
    async with _db_session.AsyncSessionLocal() as s:
        await s.execute(update(Job).where(Job.id == job_id).values(status=new_status))
        await s.commit()


async def _wait_for_active(user_id: str, expected: int, timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    active = await sse_leases.active_count(user_id)
    while active != expected and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        active = await sse_leases.active_count(user_id)
    return active


@pytest.fixture
async def live_server():
    """The real app on an ephemeral 127.0.0.1 port, in the test event loop."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await task


async def _open_stream(
    client: httpx.AsyncClient, base: str, job_id: str, token: str, redis_client: sync_redis.Redis,
) -> httpx.Response:
    """Open the stream and return once the server is subscribed to the job's channel."""
    req = client.build_request(
        "GET", f"{base}/jobs/{job_id}/logs",
        headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
    )
    async with asyncio.timeout(10):
        resp = await client.send(req, stream=True)
        assert resp.status_code == 200
        while redis_client.pubsub_numsub(f"job_logs:{job_id}")[0][1] < 1:
            await asyncio.sleep(0.05)
    return resp


async def _read_to_end(resp: httpx.Response) -> str:
    """Everything the server sends until it ends the stream."""
    received = ""
    async with asyncio.timeout(10):
        async for chunk in resp.aiter_text():
            received += chunk
    return received


# ─── Lease primitives ─────────────────────────────────────────────────────────

async def test_cap_is_per_user_and_never_blocks_another_user():
    user_a, user_b = str(uuid.uuid4()), str(uuid.uuid4())
    for _ in range(CAP):
        assert (await sse_leases.acquire(user_a)).admitted

    refused = await sse_leases.acquire(user_a)
    assert not refused.admitted
    assert refused.active == CAP
    assert 1 <= refused.retry_after_seconds <= sse_leases.LEASE_TTL_SECONDS

    assert (await sse_leases.acquire(user_b)).admitted


async def test_concurrent_opens_cannot_overshoot_the_cap():
    user_id = str(uuid.uuid4())
    results = await asyncio.gather(*(sse_leases.acquire(user_id) for _ in range(CAP * 4)))
    assert sum(1 for r in results if r.admitted) == CAP
    assert await sse_leases.active_count(user_id) == CAP


async def test_release_frees_the_slot():
    user_id = str(uuid.uuid4())
    leases = [await sse_leases.acquire(user_id) for _ in range(CAP)]
    assert not (await sse_leases.acquire(user_id)).admitted

    await sse_leases.release(user_id, leases[0].lease_id)
    assert (await sse_leases.acquire(user_id)).admitted


async def test_a_lease_that_was_never_released_expires(redis_client: sync_redis.Redis):
    """A stream killed with its process (deploy) never runs release()."""
    user_id = str(uuid.uuid4())
    leases = [await sse_leases.acquire(user_id) for _ in range(CAP)]
    assert not (await sse_leases.acquire(user_id)).admitted

    # The dead stream's lease reaches its expiry without being renewed.
    redis_client.zadd(_leases_key(user_id), {leases[0].lease_id: time.time() - 1})

    assert (await sse_leases.acquire(user_id)).admitted


async def test_refused_attempts_do_not_extend_a_lockout(redis_client: sync_redis.Redis):
    """The old counter re-armed its TTL on every refusal, so retrying kept a user locked out."""
    user_id = str(uuid.uuid4())
    for _ in range(CAP):
        await sse_leases.acquire(user_id)
    before = redis_client.zrange(_leases_key(user_id), 0, -1, withscores=True)

    for _ in range(3):
        assert not (await sse_leases.acquire(user_id)).admitted

    assert redis_client.zrange(_leases_key(user_id), 0, -1, withscores=True) == before


async def test_renew_extends_a_live_lease_but_not_a_reclaimed_one(redis_client: sync_redis.Redis):
    user_id = str(uuid.uuid4())
    lease = await sse_leases.acquire(user_id)
    old_expiry = redis_client.zscore(_leases_key(user_id), lease.lease_id)

    assert await sse_leases.renew(user_id, lease.lease_id)
    assert redis_client.zscore(_leases_key(user_id), lease.lease_id) >= old_expiry

    # Expired but not yet purged by any admission: renewing must not revive it,
    # because an admission may already have counted its slot as free.
    redis_client.zadd(_leases_key(user_id), {lease.lease_id: time.time() - 1})
    assert not await sse_leases.renew(user_id, lease.lease_id)
    assert redis_client.zscore(_leases_key(user_id), lease.lease_id) is None


# ─── Worker ordering the stream relies on ────────────────────────────────────

def _next_message(pubsub, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if message:
            return message
    return None


async def test_worker_log_line_is_stored_before_it_is_published(pending_job: Job, redis_client: sync_redis.Redis):
    """The stream subscribes, then reads stored lines. That only misses nothing
    if a line is committed before it is published."""
    pubsub = redis_client.pubsub()
    pubsub.subscribe(f"job_logs:{pending_job.id}")
    _next_message(pubsub, 0.3)  # drain the subscribe confirmation

    _publish_log(redis_client, pending_job.id, "info", "stored first", db=None)

    message = _next_message(pubsub)
    assert message is not None
    async with _db_session.AsyncSessionLocal() as s:
        row = await s.get(JobLog, json.loads(message["data"])["id"])
    assert row is not None and row.message == "stored first"

    # A line whose row cannot be stored is never published.
    missing_job_id = str(uuid.uuid4())
    pubsub.subscribe(f"job_logs:{missing_job_id}")
    _next_message(pubsub, 0.3)
    with pytest.raises(IntegrityError):
        _publish_log(redis_client, missing_job_id, "info", "never stored", db=None)
    assert _next_message(pubsub, 1.0) is None
    pubsub.close()


# ─── Route admission ──────────────────────────────────────────────────────────

async def test_over_cap_returns_429_with_retry_after(
    client: AsyncClient, starter_user: User, starter_token: str, pending_job: Job,
):
    for _ in range(CAP):
        await sse_leases.acquire(str(starter_user.id))

    resp = await client.get(
        f"/jobs/{pending_job.id}/logs",
        headers={"Authorization": f"Bearer {starter_token}", "Accept": "text/event-stream"},
    )

    assert resp.status_code == 429
    assert 1 <= int(resp.headers["Retry-After"]) <= sse_leases.LEASE_TTL_SECONDS
    assert await sse_leases.active_count(str(starter_user.id)) == CAP


async def test_finished_job_replays_without_taking_a_slot(
    client: AsyncClient, starter_user: User, starter_token: str, pending_job: Job,
):
    await _set_job_status(pending_job.id, "done")
    for _ in range(CAP):
        await sse_leases.acquire(str(starter_user.id))

    resp = await client.get(
        f"/jobs/{pending_job.id}/logs",
        headers={"Authorization": f"Bearer {starter_token}", "Accept": "text/event-stream"},
    )

    assert resp.status_code == 200
    events = [json.loads(line[len("data: "):]) for line in resp.text.split("\n") if line.startswith("data: ")]
    assert events[-1] == {"type": "done"}
    assert await sse_leases.active_count(str(starter_user.id)) == CAP


async def test_another_tenants_job_is_404_and_takes_no_slot(
    client: AsyncClient, business_user: User, business_token: str, pending_job: Job,
):
    resp = await client.get(
        f"/jobs/{pending_job.id}/logs",
        headers={"Authorization": f"Bearer {business_token}", "Accept": "text/event-stream"},
    )

    assert resp.status_code == 404
    assert await sse_leases.active_count(str(business_user.id)) == 0


# ─── Stream lifecycle over a real socket ─────────────────────────────────────

async def test_client_disconnect_releases_the_slot(
    live_server: str, starter_user: User, starter_token: str, pending_job: Job,
    redis_client: sync_redis.Redis,
):
    user_id = str(starter_user.id)
    async with httpx.AsyncClient(timeout=None) as c:
        resp = await _open_stream(c, live_server, pending_job.id, starter_token, redis_client)
        assert await sse_leases.active_count(user_id) == 1
        await resp.aclose()

    assert await _wait_for_active(user_id, 0) == 0


async def test_repeated_refreshes_never_exhaust_the_cap(
    live_server: str, starter_user: User, starter_token: str, pending_job: Job,
    redis_client: sync_redis.Redis,
):
    """The reported bug: one tab refreshed CAP times was refused on the next load."""
    user_id = str(starter_user.id)
    for _ in range(CAP * 2):
        async with httpx.AsyncClient(timeout=None) as c:
            resp = await _open_stream(c, live_server, pending_job.id, starter_token, redis_client)
            await resp.aclose()
        assert await _wait_for_active(user_id, 0) == 0


async def test_terminal_event_ends_the_stream_and_releases_the_slot(
    live_server: str, starter_user: User, starter_token: str, pending_job: Job,
    redis_client: sync_redis.Redis,
):
    user_id = str(starter_user.id)
    async with httpx.AsyncClient(timeout=None) as c:
        resp = await _open_stream(c, live_server, pending_job.id, starter_token, redis_client)
        redis_client.publish(f"job_logs:{pending_job.id}", json.dumps({"type": "done", "record_count": 0}))
        received = await _read_to_end(resp)

    assert '"type": "done"' in received
    assert await _wait_for_active(user_id, 0) == 0


async def test_job_terminalized_without_an_event_ends_the_stream(
    monkeypatch: pytest.MonkeyPatch,
    live_server: str, starter_user: User, starter_token: str, pending_job: Job,
    redis_client: sync_redis.Redis,
):
    """Cancellation publishes nothing; the stream must not hold its slot for 30 minutes."""
    monkeypatch.setattr(jobs_routes, "_SSE_STATUS_CHECK_SECONDS", 0.5)
    user_id = str(starter_user.id)
    async with httpx.AsyncClient(timeout=None) as c:
        resp = await _open_stream(c, live_server, pending_job.id, starter_token, redis_client)
        await _set_job_status(pending_job.id, "cancelled")
        received = await _read_to_end(resp)

    assert '{"type": "cancelled"}' in received
    assert await _wait_for_active(user_id, 0) == 0
