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

    # TTL on the positive Redis cache. Must NOT exceed the max JWT
    # lifetime — if a stale positive entry survives longer than the
    # JWT it could be used to keep an old revocation visible after
    # the durable DB row has been changed (admin manually clearing
    # users.revoked_at, etc.). 7 days matches the max refresh-token
    # lifetime in src/api/auth.py.
    _REVOKE_CACHE_TTL_SECONDS = 7 * 24 * 3600

    @staticmethod
    async def revoke_all_for_user(user_id: str) -> None:
        """Store a revoke timestamp for the user, durably.

        Writes to BOTH the Postgres `users.revoked_at` column (durable
        source of truth) AND a Redis cache key (hot read path). The DB
        write is awaited first; if it succeeds and Redis fails, the
        revocation still takes effect because get_user_revoke_time
        always re-validates against the DB on Redis miss.

        If the Redis SETEX raises, the previous cached value would
        otherwise stay live until TTL — including a stale "0"
        (never-revoked) sentinel if the user had been auth-checked
        recently. To prevent that fail-open hole we issue a Redis DEL
        for the key when SETEX fails, forcing the next reader to hit
        the DB and observe the now-durable revocation. If even DEL
        fails, the durable DB row is still authoritative once Redis
        recovers (the next reader's GET will return the stale value
        BUT we re-validate against DB because the negative-cache
        sentinel is no longer trusted — see get_user_revoke_time).

        Raises on Postgres error so /auth/logout-all returns 5xx and
        the client retries instead of reporting a successful revocation
        that did not persist.
        """
        from sqlalchemy import update
        from datetime import datetime, UTC
        from src.db.models import User
        from src.db.session import async_engine

        now = datetime.now(UTC)
        # 1) Durable write to users.revoked_at — must succeed.
        async with async_engine.begin() as conn:
            await conn.execute(
                update(User).where(User.id == user_id).values(revoked_at=now)
            )

        # 2) Best-effort Redis write. If it fails, invalidate the key
        # so a stale "0" sentinel cannot mask the now-durable revocation.
        key = f"{TokenBlacklist._USER_REVOKE_PREFIX}{user_id}"
        try:
            r = _get_redis()
            await r.setex(
                key, TokenBlacklist._REVOKE_CACHE_TTL_SECONDS, str(int(now.timestamp())),
            )
        except redis_exceptions.RedisError as exc:
            _logger.warning(
                "revoke_all_for_user: Redis SETEX failed; attempting DEL to invalidate stale cache: %s",
                exc,
            )
            try:
                await _get_redis().delete(key)
            except redis_exceptions.RedisError as del_exc:
                # If both SETEX and DEL fail, the cache may briefly
                # serve a stale negative sentinel until the entry
                # expires. Log loud — the DB row IS revoked, but
                # ops should know auth-cache writes are failing.
                _logger.error(
                    "revoke_all_for_user: BOTH Redis SETEX and DEL failed; "
                    "cache may briefly mask DB revocation for user %s: %s",
                    user_id, del_exc,
                )

    @staticmethod
    async def get_user_revoke_time(user_id: str) -> int:
        """Return the epoch timestamp at which all tokens for this user were revoked (0 if never).

        Reads Redis first (microsecond hot path) and uses the cached
        value when present. On Redis cache MISS we always consult the
        DB authoritatively — we do NOT negative-cache the never-revoked
        state because a stale "0" sentinel could mask a logout-all that
        wrote durably to Postgres but failed to update Redis.
        Steady-state cost: one indexed PK lookup per JWT validation
        when Redis hasn't seen this user yet, accepted as the price of
        making revoke_all_for_user safe under partial Redis failure.

        On RedisError, also falls back to the DB. The 503 surface in
        the caller now only fires if Redis is unavailable AND the DB
        query also raises. ProgrammingError (column-missing — happens
        if code lands before migration 024) is one such DB failure
        and we deliberately do NOT swallow it as 0; treating a
        schema-rollout race as fail-open would let revoked sessions
        keep serving until Redis re-populates. Production deploys go
        through start.sh which blocks uvicorn until alembic upgrade
        head succeeds, so the rollout race shouldn't reach this path.
        """
        key = f"{TokenBlacklist._USER_REVOKE_PREFIX}{user_id}"
        try:
            r = _get_redis()
            val = await r.get(key)
            if val is not None:
                # Trust ONLY positive cached timestamps. A "0" sentinel
                # is no longer written by this code, but pre-deploy
                # commits (specifically 6b74d5a, the brief window
                # before this stale-0 fix) wrote ts=0 to Upstash with
                # an 8-day TTL. Until those entries naturally expire,
                # honoring them would let a successful /logout-all
                # whose cache write happened to fail be silently
                # masked. Treating "0" as an untrusted miss closes
                # that window: the next read goes to the DB and
                # observes the durable users.revoked_at row.
                try:
                    cached_ts = int(val)
                except (TypeError, ValueError):
                    cached_ts = 0
                if cached_ts > 0:
                    return cached_ts
                # Stale "0" sentinel or empty value — fall through to DB.
        except redis_exceptions.RedisError as exc:
            _logger.warning(
                "get_user_revoke_time: Redis unavailable, falling back to DB for user %s: %s",
                user_id, exc,
            )

        # DB authoritative read.
        from sqlalchemy import select
        from src.db.models import User
        from src.db.session import async_engine

        async with async_engine.connect() as conn:
            row = (await conn.execute(
                select(User.revoked_at).where(User.id == user_id)
            )).first()

        if row is None or row[0] is None:
            return 0
        ts = int(row[0].timestamp())

        # Backfill the POSITIVE case only. Never-revoked users keep
        # paying the DB hop on cache miss — that's the trade-off for
        # not having a stale-cache fail-open. Best-effort write: a
        # Redis failure here just means the next read pays the DB hop
        # again, which is the worst-case path anyway.
        try:
            r = _get_redis()
            await r.setex(key, TokenBlacklist._REVOKE_CACHE_TTL_SECONDS, str(ts))
        except redis_exceptions.RedisError:
            pass
        return ts

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
