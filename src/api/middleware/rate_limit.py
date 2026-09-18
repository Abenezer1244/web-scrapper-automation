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
    # Endpoints that make an OUTBOUND Stripe API call per request
    # (/billing/subscription, /checkout, /portal). Tighter than `general` and
    # fail-CLOSED (see _FALLBACK_ZONES): each call spends the operator's Stripe
    # quota and can spam Customer/Checkout objects, so a stolen JWT looping these
    # — even during a Redis outage — must stay throttled. 10/min per user is ample
    # for legitimate upgrade/manage flows (Codex security cross-check).
    "stripe": (10, 60),
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


# Number of trusted reverse-proxy hops in front of the app. Each trusted proxy
# APPENDS the address it received from to the RIGHT of X-Forwarded-For, so the
# real client is the Nth entry from the end. Railway/Fly = 1. (I1)
_TRUSTED_PROXY_HOPS = max(1, settings.TRUSTED_PROXY_HOPS)


def client_ip(request: Request) -> str:
    """Extract the client IP, trusting forwarded headers only from known proxies.

    I1: each trusted proxy APPENDS the address it received from to the RIGHT of
    X-Forwarded-For. With one proxy in front (Railway/Fly) the real client is
    the LAST entry; everything to its left was written by the client and is
    forgeable. The previous code took the LEFTMOST entry, so an attacker could
    send `X-Forwarded-For: <random>` on each request to mint unlimited distinct
    rate-limit keys and bypass the limit entirely — defeating /auth/login
    brute-force protection and webhook signature-spray throttling. We now take
    the Nth-from-last entry (N = trusted hops).

    Note: vendor headers like Fly-Client-IP / CF-Connecting-IP are NOT trusted
    here. They are only authentic when the app actually sits behind that
    specific vendor's edge; on Railway (the deploy target) an attacker can set
    them and the proxy passes them through, which would reintroduce the bypass.
    The only non-forgeable value behind a known proxy is the hop the proxy
    itself appended to X-Forwarded-For. If a CDN like Cloudflare is added in
    front later, validate the immediate peer or strip/normalize the header at
    the edge before trusting its vendor header.
    """
    direct_ip = request.client.host if request.client else "unknown"

    if _is_trusted_proxy(direct_ip):
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            if parts:
                hops = min(len(parts), _TRUSTED_PROXY_HOPS)
                return parts[-hops]

    return direct_ip


# I2: per-process fallback limiter for security-critical zones so a Redis
# outage cannot fully disable throttling there. Coarse (per-worker, not shared)
# but enough to stop a single worker from accepting unlimited auth/webhook
# attempts during an incident. Other zones still fail fully open (availability
# over abuse-resistance is the right trade for non-security paths).
_FALLBACK_ZONES = frozenset({"auth", "webhook", "stripe"})
# Insertion-ordered (dict preserves order), so the oldest keys are the cheapest
# to find when we need to reclaim space.
_fallback_hits: dict[str, list[float]] = {}
_FALLBACK_MAX_KEYS = 10_000


def _fallback_allow(key: str, max_requests: int, window_seconds: int, now: float) -> bool:
    """Per-process limiter used only while Redis is unavailable.

    Memory is bounded by EXPIRY-ONLY eviction, never by clearing. The previous
    `_fallback_hits.clear()` at the bound was a bypass: during a Redis outage
    this is the ONLY limiter for the auth/webhook/stripe zones, so an attacker
    could mint 10k throwaway keys and wipe every real counter — including the
    brute-force ladder on an account they were attacking. Reclaiming space must
    never discard an ACTIVE counter.

    If every slot is genuinely live, admission fails CLOSED (deny the new key)
    rather than evicting someone else's. During an outage a new client seeing a
    429 is the correct trade against silently disarming the limiter for all
    existing ones. (Codex.)
    """
    cutoff = now - window_seconds
    bucket = _fallback_hits.setdefault(key, [])
    bucket[:] = [t for t in bucket if t > cutoff]

    if len(_fallback_hits) > _FALLBACK_MAX_KEYS:
        # Drop only EXPIRED buckets, oldest-first. A bucket's stale timestamps
        # are normally pruned only when ITS OWN key is next seen, so a candidate
        # must be re-filtered here before deciding it is empty — otherwise
        # nothing is ever reclaimable and one historical flood denies every new
        # caller forever. (Caught by
        # test_expired_buckets_are_reclaimed_so_normal_traffic_is_not_denied.)
        #
        # Using this call's cutoff is sound because every _FALLBACK_ZONES zone
        # shares the same 60s window. Adding a fallback zone with a LONGER window
        # would make this prune another zone's entries early — widen the cutoff
        # here if that ever happens.
        for k in list(_fallback_hits):
            if len(_fallback_hits) <= _FALLBACK_MAX_KEYS:
                break
            if k == key:
                continue
            other = _fallback_hits[k]
            if other and other[-1] > cutoff:
                continue  # still live, leave it alone
            del _fallback_hits[k]
        if len(_fallback_hits) > _FALLBACK_MAX_KEYS:
            # Still full of live counters → refuse the newcomer instead of
            # evicting an active one. Do not leave an empty bucket behind.
            if not bucket:
                _fallback_hits.pop(key, None)
            return False

    bucket.append(now)
    return len(bucket) <= max_requests


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
        # I2: for security-critical zones, fall back to a per-process limiter
        # instead of fully failing open — a Redis incident must not hand an
        # attacker an unthrottled /auth/login or webhook-spray window.
        if zone in _FALLBACK_ZONES and not _fallback_allow(
            f"{zone}:{key_id}", max_requests, window_seconds, now
        ):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please slow down.",
                headers={"Retry-After": str(window_seconds)},
            )
        return

    request_count: int = results[2]

    if request_count > max_requests:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please slow down.",
            headers={"Retry-After": str(window_seconds)},
        )


async def once_per(key: str, ttl_seconds: int) -> bool:
    """Idempotency gate: True the FIRST time `key` is seen within `ttl_seconds`,
    False on every repeat until the key expires.

    Backed by Redis ``SET key 1 NX EX ttl`` (atomic test-and-set). Use to throttle
    a side effect that must fire at most once per window per subject — e.g. one
    duplicate-signup email per email-address per day, regardless of how many times
    the address is submitted (an email-bomb guard that, unlike per-IP limiting,
    an attacker cannot evade by rotating IPs).

    Fails CLOSED (returns False) if Redis is unavailable: the gated side effect is
    non-critical, so skipping it during an outage is strictly safer than letting it
    fire unthrottled. This is the opposite of rate_limit()'s fail-OPEN posture,
    which protects availability of the request path; here we protect the subject
    from spam.
    """
    redis_key = f"once:{key}"
    try:
        # nx=True -> only set if absent; returns True when set, None when it existed.
        was_set = await _get_redis().set(redis_key, "1", nx=True, ex=ttl_seconds)
    except redis_exceptions.RedisError as exc:
        _logger.warning("once_per fail-closed: Redis error for key=%s: %s", redis_key, exc)
        return False
    return bool(was_set)


async def release_once(key: str) -> None:
    """Release a gate previously claimed by once_per(), so the NEXT call for the
    same key can fire again immediately.

    Use when the side effect that once_per() gated could not actually be started
    (e.g. the task enqueue raised right after the gate was claimed) — otherwise a
    single transient failure would suppress the side effect for the whole TTL.
    Best-effort: a failed delete just means the gate expires on its own TTL.
    """
    try:
        await _get_redis().delete(f"once:{key}")
    except redis_exceptions.RedisError as exc:
        _logger.warning("release_once: Redis error for key=once:%s: %s", key, exc)
