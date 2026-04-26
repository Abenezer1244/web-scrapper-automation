"""Redis sliding window rate limiter.

Usage:
    from src.api.middleware.rate_limit import rate_limit

    @router.post("/auth/login")
    async def login(request: Request, ...):
        await rate_limit(request, zone="auth")
"""

import ipaddress
import logging
import time

import redis.asyncio as aioredis
import redis.exceptions as redis_exceptions
from fastapi import HTTPException, Request, status

from src.config import settings

_logger = logging.getLogger("security.rate_limit")

# Zone config: (max_requests, window_seconds)
_ZONES: dict[str, tuple[int, int]] = {
    "auth": (10, 60),       # 10 req/min per IP
    "jobs": (5, 60),        # 5 job creations/min per user
    "general": (60, 60),    # 60 req/min per IP
    # C5 (full-SaaS review): webhook endpoints were unthrottled.
    # Legitimate Stripe delivers ~1-2 events/sec to a busy account
    # with retries; Tracerfy webhooks fire once per batch completion
    # (minutes apart). 120 req/min per source IP is ample headroom
    # for both while still blocking attackers who spray invalid
    # signatures to burn CPU on HMAC verification.
    "webhook": (120, 60),
}

_redis_client: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
    return _redis_client


_TRUSTED_PROXY_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _is_trusted_proxy(ip_str: str) -> bool:
    """Check if an IP belongs to a known proxy / private network."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(addr in net for net in _TRUSTED_PROXY_NETWORKS)


def client_ip(request: Request) -> str:
    """Extract client IP. Only trust forwarded headers from known proxies."""
    direct_ip = request.client.host if request.client else "unknown"

    # Only trust X-Forwarded-For if request came from a known proxy (Railway, Docker)
    if _is_trusted_proxy(direct_ip):
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        for header in ("Fly-Client-IP", "CF-Connecting-IP", "X-Real-IP"):
            val = request.headers.get(header)
            if val:
                return val.strip()

    return direct_ip


async def rate_limit(request: Request, zone: str = "general", identifier: str | None = None) -> None:
    """Raises HTTP 429 if the caller exceeds the zone's rate limit.

    Args:
        request: The incoming FastAPI request.
        zone: One of 'auth', 'jobs', 'general'.
        identifier: Custom key (e.g. user_id). Falls back to client IP.
    """
    max_requests, window_seconds = _ZONES.get(zone, _ZONES["general"])
    key_id = identifier or client_ip(request)
    redis_key = f"rl:{zone}:{key_id}"

    now = time.time()
    window_start = now - window_seconds

    r = _get_redis()
    pipe = r.pipeline()
    pipe.zremrangebyscore(redis_key, "-inf", window_start)
    pipe.zadd(redis_key, {str(now): now})
    pipe.zcard(redis_key)
    pipe.expire(redis_key, window_seconds)
    try:
        results = await pipe.execute()
    except redis_exceptions.RedisError as exc:
        # Rate limiting is best-effort defense — if the limiter itself
        # cannot run (Upstash quota throttle, network blip, connection
        # pool exhaustion, ...) we MUST fail open. Failing closed turns
        # every request into a 500 and takes the entire API down for
        # the duration of the Redis incident, which is exactly what we
        # observed when Upstash rate-limited the project's own DB and
        # /auth/login started returning Internal Server Error to users.
        # The log + audit trail here is enough for ops to notice and
        # investigate without taking real users offline.
        _logger.warning(
            "rate_limit fail-open: Redis error while checking zone=%s key=%s: %s",
            zone, redis_key, exc,
        )
        return

    request_count: int = results[2]

    if request_count > max_requests:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please slow down.",
            headers={"Retry-After": str(window_seconds)},
        )
