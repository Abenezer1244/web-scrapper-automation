"""Per-user admission for the live job log stream (SSE).

Each open stream holds a LEASE: a member of the Redis sorted set
``sse_leases:{user_id}`` whose score is the lease's expiry (epoch seconds).
Admission purges expired members, counts the rest and adds the new lease in
one Lua script, so concurrent opens cannot overshoot the cap.

Why leases and not a counter (2026-09-13): the previous INCR/DECR counter
decremented in the stream's ``finally``. When the browser disconnects,
Starlette cancels the stream task and anyio re-cancels every await inside
that ``finally``, so the DECR never ran. Every refresh, route change or tab
close leaked a slot. One tab reloaded four times locked its user out with a
single real stream, and each rejected retry re-armed the counter's TTL. The
counter key also expired under streams older than 120s, so a user could hold
10 live streams with a cap of 5.

With leases, correctness never depends on cleanup completing. ``release()``
is the fast path and runs shielded from cancellation; a lease whose release
never ran (process killed by a deploy, Redis blip) simply expires.
"""

import logging
import math
import uuid
from dataclasses import dataclass

import anyio
import redis.asyncio as aioredis

from src.config import settings

_logger = logging.getLogger("api.sse_leases")

# A live stream renews its lease every HEARTBEAT seconds. TTL leaves room for
# two missed renewals before a dead stream's slot is reclaimed.
LEASE_HEARTBEAT_SECONDS = 20
LEASE_TTL_SECONDS = 60

_redis_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """Shared async client for the stream route (leases + Pub/Sub)."""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
    return _redis_client


def _key(user_id: str) -> str:
    return f"sse_leases:{user_id}"


# KEYS[1] lease set. ARGV: now, expiry, cap, lease id, key ttl.
# Returns {admitted (1/0), active count, earliest expiry or 0}.
_ADMIT_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
local active = redis.call('ZCARD', key)
if active >= tonumber(ARGV[3]) then
  local first = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  return {0, active, first[2] or '0'}
end
redis.call('ZADD', key, ARGV[2], ARGV[4])
redis.call('EXPIRE', key, ARGV[5])
return {1, active + 1, '0'}
"""

# Renews only a lease that is still unexpired. An expired lease that no
# admission has purged yet must not come back: its slot may already be
# counted as free, so the stream re-admits through the cap instead.
# KEYS[1] lease set. ARGV: now, new expiry, lease id, key ttl.
_RENEW_LUA = """
local score = redis.call('ZSCORE', KEYS[1], ARGV[3])
if score and tonumber(score) > tonumber(ARGV[1]) then
  redis.call('ZADD', KEYS[1], 'XX', ARGV[2], ARGV[3])
  redis.call('EXPIRE', KEYS[1], ARGV[4])
  return 1
end
if score then
  redis.call('ZREM', KEYS[1], ARGV[3])
end
return 0
"""

# Bound on each cleanup call so a hung Redis cannot pin a finished stream task.
_CLEANUP_TIMEOUT_SECONDS = 5


async def _redis_now(r: aioredis.Redis) -> float:
    """Redis's clock, so every API replica compares leases on one clock.

    Read with its own command rather than TIME inside the scripts, which some
    managed Redis providers restrict in scripts that also write. The read and
    the script are two round trips, so "now" can be stale by the gap between
    them; misjudging a lease that way needs a stall of about 40 seconds (a
    lease is renewed at 20s of its 60s TTL), so it is accepted.
    """
    seconds, microseconds = await r.time()
    return int(seconds) + int(microseconds) / 1_000_000


@dataclass(frozen=True)
class Admission:
    lease_id: str | None
    active: int
    retry_after_seconds: int

    @property
    def admitted(self) -> bool:
        return self.lease_id is not None


async def acquire(user_id: str) -> Admission:
    """Admit a new stream for ``user_id`` if they hold fewer than the cap."""
    r = get_redis()
    now = await _redis_now(r)
    lease_id = uuid.uuid4().hex
    admitted, active, earliest = await r.eval(
        _ADMIT_LUA,
        1,
        _key(user_id),
        now,
        now + LEASE_TTL_SECONDS,
        settings.SSE_MAX_STREAMS_PER_USER,
        lease_id,
        LEASE_TTL_SECONDS * 2,
    )
    if int(admitted) == 1:
        return Admission(lease_id=lease_id, active=int(active), retry_after_seconds=0)
    retry_after = max(1, math.ceil(float(earliest) - now)) if float(earliest) else LEASE_TTL_SECONDS
    return Admission(lease_id=None, active=int(active), retry_after_seconds=retry_after)


async def renew(user_id: str, lease_id: str) -> bool:
    """Extend a live lease. False means it already expired and was reclaimed."""
    r = get_redis()
    now = await _redis_now(r)
    renewed = await r.eval(
        _RENEW_LUA,
        1,
        _key(user_id),
        now,
        now + LEASE_TTL_SECONDS,
        lease_id,
        LEASE_TTL_SECONDS * 2,
    )
    return int(renewed) == 1


async def release(user_id: str, lease_id: str) -> None:
    """Free the slot now. Shielded so a cancelled stream still releases.

    Never raises: expiry reclaims the slot if this fails or times out, and an
    error here must not mask whatever ended the stream.
    """
    with anyio.move_on_after(_CLEANUP_TIMEOUT_SECONDS, shield=True) as scope:
        try:
            await get_redis().zrem(_key(user_id), lease_id)
        except Exception:
            _logger.warning(
                "sse lease release failed user=%s lease=%s; it will expire in <= %ss",
                user_id, lease_id, LEASE_TTL_SECONDS, exc_info=True,
            )
    if scope.cancelled_caught:
        _logger.warning(
            "sse lease release timed out user=%s lease=%s; it will expire in <= %ss",
            user_id, lease_id, LEASE_TTL_SECONDS,
        )


async def close_pubsub(pubsub: aioredis.client.PubSub, job_id: str) -> None:
    """Close a stream's Pub/Sub connection, shielded and bounded. Never raises."""
    with anyio.move_on_after(_CLEANUP_TIMEOUT_SECONDS, shield=True) as scope:
        try:
            await pubsub.aclose()
        except Exception:
            _logger.warning("sse pubsub close failed job=%s", job_id, exc_info=True)
    if scope.cancelled_caught:
        _logger.warning("sse pubsub close timed out job=%s", job_id)


async def active_count(user_id: str) -> int:
    """Unexpired leases for ``user_id`` (observability and tests)."""
    r = get_redis()
    return int(await r.zcount(_key(user_id), await _redis_now(r), "+inf"))
