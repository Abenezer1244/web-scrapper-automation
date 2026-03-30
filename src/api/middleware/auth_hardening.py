"""Auth hardening: token blacklist, brute-force protection, constant-time comparison."""

import hmac
import time

import redis.asyncio as aioredis
from fastapi import HTTPException, status

from src.config import settings

_redis_client: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        kwargs: dict = {"decode_responses": True}
        if settings.REDIS_URL.startswith("rediss://"):
            kwargs["ssl_cert_reqs"] = None  # Upstash uses custom certs not in system CA
        _redis_client = aioredis.from_url(settings.REDIS_URL, **kwargs)
    return _redis_client


# ─── Constant-time comparison ─────────────────────────────────────────────────

def constant_time_compare(val1: str, val2: str) -> bool:
    """Compare two strings in constant time to prevent timing attacks."""
    return hmac.compare_digest(val1.encode(), val2.encode())


# ─── Token blacklist ──────────────────────────────────────────────────────────

class TokenBlacklist:
    """Redis-backed JWT blacklist.

    When a user logs out, the token's jti is added to Redis with a TTL
    matching the token's remaining lifetime.
    """

    _KEY_PREFIX = "bl:jti:"
    _USER_REVOKE_PREFIX = "bl:user_revoke:"

    @staticmethod
    async def add(jti: str, expires_in_seconds: int) -> None:
        """Blacklist a single JWT by its jti."""
        r = _get_redis()
        key = f"{TokenBlacklist._KEY_PREFIX}{jti}"
        await r.setex(key, expires_in_seconds, "1")

    @staticmethod
    async def is_blacklisted(jti: str) -> bool:
        """Return True if this jti has been blacklisted."""
        r = _get_redis()
        key = f"{TokenBlacklist._KEY_PREFIX}{jti}"
        return await r.exists(key) == 1

    @staticmethod
    async def revoke_all_for_user(user_id: str) -> None:
        """Store a revoke timestamp for the user.

        Any token issued before this timestamp is considered revoked.
        JWT decoder must check this alongside the blacklist.
        """
        r = _get_redis()
        key = f"{TokenBlacklist._USER_REVOKE_PREFIX}{user_id}"
        # Keep the timestamp for 8 days (longer than max token lifetime of 7 days)
        await r.setex(key, 8 * 24 * 3600, str(int(time.time())))

    @staticmethod
    async def get_user_revoke_time(user_id: str) -> int:
        """Return the epoch timestamp at which all tokens for this user were revoked (0 if never)."""
        r = _get_redis()
        key = f"{TokenBlacklist._USER_REVOKE_PREFIX}{user_id}"
        val = await r.get(key)
        return int(val) if val else 0


# ─── Brute-force protection ───────────────────────────────────────────────────

_LOCKOUT_THRESHOLDS = [
    (5,  1 * 60),    # 5 failures  → 1 min lockout
    (10, 5 * 60),    # 10 failures → 5 min lockout
    (20, 30 * 60),   # 20 failures → 30 min lockout
    (50, 24 * 3600), # 50 failures → 24 hr lockout
]


class BruteForceProtection:
    """Per-IP and per-email progressive lockout.

    Tracks failure counts independently for the client IP and the target
    email so that distributed attacks (many IPs, one email) are also caught.
    """

    _KEY_PREFIX = "bf:"

    @staticmethod
    def _lockout_duration(failures: int) -> int:
        """Return lockout seconds for a given failure count (0 = not locked out)."""
        duration = 0
        for threshold, seconds in _LOCKOUT_THRESHOLDS:
            if failures >= threshold:
                duration = seconds
        return duration

    @staticmethod
    async def check(ip: str, email: str) -> None:
        """Raise HTTP 429 if either the IP or email is locked out."""
        r = _get_redis()
        for key_suffix in [f"ip:{ip}", f"email:{email}"]:
            key = f"{BruteForceProtection._KEY_PREFIX}{key_suffix}"
            val = await r.get(key)
            failures = int(val) if val else 0
            lockout = BruteForceProtection._lockout_duration(failures)
            if lockout > 0:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many failed login attempts. Try again later.",
                    headers={"Retry-After": str(lockout)},
                )

    @staticmethod
    async def record_failure(ip: str, email: str) -> None:
        """Increment failure counters for both the IP and email."""
        r = _get_redis()
        for key_suffix, ttl in [
            (f"ip:{ip}", 24 * 3600),
            (f"email:{email}", 24 * 3600),
        ]:
            key = f"{BruteForceProtection._KEY_PREFIX}{key_suffix}"
            await r.incr(key)
            await r.expire(key, ttl)

    @staticmethod
    async def clear(ip: str, email: str) -> None:
        """Clear failure counters on successful login."""
        r = _get_redis()
        await r.delete(
            f"{BruteForceProtection._KEY_PREFIX}ip:{ip}",
            f"{BruteForceProtection._KEY_PREFIX}email:{email}",
        )
