"""Auth hardening: token blacklist + brute-force protection (Redis-backed)."""

import logging
import time

import redis.asyncio as aioredis
import redis.exceptions as redis_exceptions
from fastapi import HTTPException, status

from src.config import settings

_logger = logging.getLogger("security.auth_hardening")

_redis_client: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
    return _redis_client


# ─── Token blacklist ──────────────────────────────────────────────────────────


def revocation_unavailable_503() -> HTTPException:
    """Build the 503 response used everywhere a revocation check failed.

    Centralised so every TokenBlacklist call site that catches
    ``redis.exceptions.RedisError`` surfaces an identical, parsable
    response — the frontend can match on this status + Retry-After
    header to render a banner instead of a hard logout.
    """
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Authentication service temporarily unavailable. Please try again shortly.",
        headers={"Retry-After": "30"},
    )


class TokenBlacklist:
    """Redis-backed JWT blacklist.

    When a user logs out, the token's jti is added to Redis with a TTL
    matching the token's remaining lifetime.
    """

    _KEY_PREFIX = "bl:jti:"
    _USER_REVOKE_PREFIX = "bl:user_revoke:"

    @staticmethod
    async def add(jti: str, expires_in_seconds: int) -> None:
        """Blacklist a single JWT by its jti.

        Raises if Redis is unavailable — caller (logout flow) should
        return a 503 rather than silently fail to revoke a token.
        """
        r = _get_redis()
        key = f"{TokenBlacklist._KEY_PREFIX}{jti}"
        await r.setex(key, expires_in_seconds, "1")

    @staticmethod
    async def is_blacklisted(jti: str) -> bool:
        """Return True if this jti has been blacklisted.

        Fails CLOSED on Redis errors. Revocation is a SECURITY BOUNDARY:
        if we cannot verify whether a token has been revoked, we MUST
        NOT serve the request — silently accepting revoked tokens
        during a Redis outage would re-authorize logged-out users and
        any tokens an attacker has stolen since the last revocation.
        The caller (JWT decoder in src/api/auth.py) catches the
        RedisError and surfaces it as 503 so the client distinguishes
        an infrastructure outage from an authentication failure.

        Trade-off vs. the rate-limit / brute-force counters (which
        DO fail open): rate-limiting and lockout counters are
        best-effort defense in depth — losing them temporarily
        weakens the system's resistance to abuse but does not
        unauthorize anyone. Revocation is the opposite: it is the
        primary mechanism that separates "logged in" from "logged
        out", so it must hold even when our infra is unhealthy.
        """
        r = _get_redis()
        key = f"{TokenBlacklist._KEY_PREFIX}{jti}"
        return await r.exists(key) == 1

    @staticmethod
    async def revoke_all_for_user(user_id: str) -> None:
        """Store a revoke timestamp for the user.

        Any token issued before this timestamp is considered revoked.
        JWT decoder must check this alongside the blacklist. Raises on
        Redis error — caller should surface a 503 rather than report
        success without actually revoking.
        """
        r = _get_redis()
        key = f"{TokenBlacklist._USER_REVOKE_PREFIX}{user_id}"
        # Keep the timestamp for 8 days (longer than max token lifetime of 7 days)
        await r.setex(key, 8 * 24 * 3600, str(int(time.time())))

    @staticmethod
    async def get_user_revoke_time(user_id: str) -> int:
        """Return the epoch timestamp at which all tokens for this user were revoked (0 if never).

        Fails CLOSED on Redis errors. Same security-boundary rationale
        as is_blacklisted — if we cannot read the global revocation
        timestamp, we cannot tell whether a token was issued before a
        password reset / account compromise / "log out everywhere"
        action. Failing open here would re-authorize any token issued
        before the revocation for the duration of the Redis outage.
        Caller catches the RedisError and surfaces 503.
        """
        r = _get_redis()
        key = f"{TokenBlacklist._USER_REVOKE_PREFIX}{user_id}"
        val = await r.get(key)
        return int(val) if val else 0

    @staticmethod
    async def is_revoked_by_user_logout_all(user_id: str, issued_at: int) -> bool:
        """True if `user_id` has done logout-all and the token was issued at or before that moment.

        Use ``issued_at <= revoke_time`` (NOT strict ``<``) — token
        timestamps and the revoke timestamp are both whole seconds, so
        a token whose ``iat`` lands in the same second as the
        logout-all call is ambiguous about ordering. Strict ``<``
        would silently let those tokens survive a logout-all,
        defeating the entire purpose of the call. The cost is that a
        legitimate login in the exact same second as logout-all may
        need to be retried once — a rare, recoverable UX hiccup —
        traded for closing a real security bypass.

        Caller is responsible for catching ``RedisError`` (we let it
        propagate so failure surfaces as 503, not as "not revoked").
        """
        revoke_time = await TokenBlacklist.get_user_revoke_time(user_id)
        return revoke_time > 0 and issued_at <= revoke_time


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
        """Raise HTTP 429 if either the IP or email is locked out.

        Fails OPEN on Redis errors. Brute-force lockout is best-effort
        defense layered on top of the password hash check; if Redis is
        unavailable we cannot tell whether the caller is locked out, but
        failing CLOSED would 500 every login attempt for the duration
        of the Redis incident — taking real users offline. Failing open
        re-exposes the attack surface for the outage window only, which
        is preferable to a full auth outage.
        """
        r = _get_redis()
        try:
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
        except redis_exceptions.RedisError as exc:
            _logger.warning(
                "BruteForceProtection.check fail-open: Redis error for ip=%s email=%s: %s",
                ip, email, exc,
            )
            return

    _NOTIFY_THRESHOLD = 10  # Send email after this many failures

    @staticmethod
    async def record_failure(ip: str, email: str) -> None:
        """Increment failure counters for both the IP and email.

        Sends a one-time lockout notification email when the email-based
        counter crosses the notification threshold. Best-effort: any
        Redis error is logged and swallowed so the outer login handler
        can still return its normal 401 response instead of 500-ing.

        Each (incr, expire) pair runs inside a MULTI/EXEC transactional
        pipeline so they are applied atomically. Without this, a Redis
        blip that lands BETWEEN the two commands would leave the
        counter incremented but with no TTL — when Redis recovers,
        check() would observe a counter that never decays and lock the
        user out indefinitely.
        """
        r = _get_redis()
        email_failures = 0
        try:
            for key_suffix, ttl in [
                (f"ip:{ip}", 24 * 3600),
                (f"email:{email}", 24 * 3600),
            ]:
                key = f"{BruteForceProtection._KEY_PREFIX}{key_suffix}"
                pipe = r.pipeline(transaction=True)
                pipe.incr(key)
                pipe.expire(key, ttl)
                results = await pipe.execute()
                count = int(results[0])
                if key_suffix.startswith("email:"):
                    email_failures = count
        except redis_exceptions.RedisError as exc:
            _logger.warning(
                "BruteForceProtection.record_failure skipped (Redis error) ip=%s email=%s: %s",
                ip, email, exc,
            )
            return

        # Send lockout notification once when threshold is first crossed
        if email_failures == BruteForceProtection._NOTIFY_THRESHOLD:
            dedup_key = f"{BruteForceProtection._KEY_PREFIX}notified:{email}"
            try:
                already_sent = await r.get(dedup_key)
                if not already_sent:
                    await r.setex(dedup_key, 24 * 3600, "1")
                else:
                    return
            except redis_exceptions.RedisError as exc:
                _logger.warning(
                    "Lockout notification dedup check failed (Redis error) email=%s: %s",
                    email, exc,
                )
                return
            try:
                from src.workers.delivery import send_lockout_notification
                send_lockout_notification(email, email_failures, ip)
            except Exception as notify_exc:
                    # L10 (full-SaaS review): never let notification
                    # failure affect auth flow — but DO log it. The
                    # previous `except Exception: pass` swallowed
                    # legitimate coding errors (typo in function
                    # name, import error) as well as the intended
                    # transient email failures, making regressions
                    # invisible until the notification simply
                    # stopped firing in production.
                    import logging
                    logging.getLogger("auth.lockout").warning(
                        "Lockout notification failed for %s: %s",
                        email, str(notify_exc)[:200],
                    )

    @staticmethod
    async def clear(ip: str, email: str) -> None:
        """Clear failure counters on successful login.

        Best-effort — if Redis is unavailable the stale counter just
        decays via its 24h TTL. Failing the login because the counters
        could not be cleared would be a worse user experience.
        """
        r = _get_redis()
        try:
            await r.delete(
                f"{BruteForceProtection._KEY_PREFIX}ip:{ip}",
                f"{BruteForceProtection._KEY_PREFIX}email:{email}",
            )
        except redis_exceptions.RedisError as exc:
            _logger.warning(
                "BruteForceProtection.clear skipped (Redis error) ip=%s email=%s: %s",
                ip, email, exc,
            )
