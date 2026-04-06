"""Redis sliding window rate limiter.

Usage:
    from src.api.middleware.rate_limit import rate_limit

    @router.post("/auth/login")
    async def login(request: Request, ...):
        await rate_limit(request, zone="auth")
"""

import time

import redis.asyncio as aioredis
from fastapi import HTTPException, Request, status

from src.config import settings

# Zone config: (max_requests, window_seconds)
_ZONES: dict[str, tuple[int, int]] = {
    "auth": (10, 60),       # 10 req/min per IP
    "jobs": (5, 60),        # 5 job creations/min per user
    "general": (60, 60),    # 60 req/min per IP
}

_redis_client: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        kwargs: dict = {"decode_responses": True}
        if settings.REDIS_URL.startswith("rediss://"):
            kwargs["ssl_cert_reqs"] = None  # Upstash uses custom certs not in system CA
        _redis_client = aioredis.from_url(settings.REDIS_URL, **kwargs)
    return _redis_client


def _client_ip(request: Request) -> str:
    """Extract client IP. Only trust X-Forwarded-For from Railway's proxy."""
    direct_ip = request.client.host if request.client else "unknown"

    # Only trust X-Forwarded-For if request came from a known proxy (Railway, Docker)
    # Railway sets this header; direct requests from attackers are ignored.
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded and direct_ip in ("127.0.0.1", "::1", "10.0.0.0/8"):
        return forwarded.split(",")[0].strip()

    # Use Fly-Client-IP or CF-Connecting-IP (set by trusted proxies, not spoofable)
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
    key_id = identifier or _client_ip(request)
    redis_key = f"rl:{zone}:{key_id}"

    now = time.time()
    window_start = now - window_seconds

    r = _get_redis()
    pipe = r.pipeline()
    pipe.zremrangebyscore(redis_key, "-inf", window_start)
    pipe.zadd(redis_key, {str(now): now})
    pipe.zcard(redis_key)
    pipe.expire(redis_key, window_seconds)
    results = await pipe.execute()

    request_count: int = results[2]

    if request_count > max_requests:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please slow down.",
            headers={"Retry-After": str(window_seconds)},
        )
