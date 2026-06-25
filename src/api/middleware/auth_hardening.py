"""Auth hardening: token blacklist + brute-force protection (Redis-backed)."""

import logging
from datetime import datetime

import redis.asyncio as aioredis
import redis.exceptions as redis_exceptions
from fastapi import HTTPException, status

from src.config import settings
from src.utils.crypto import blind_index
from src.utils.logger import email_fingerprint

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
    async def consume_once(jti: str, expires_in_seconds: int) -> bool:
        """Atomically claim a jti exactly once (single-use token rotation).

        Uses Redis ``SET key 1 NX EX ttl``: returns True if THIS call set
        the key (the token had not been used or blacklisted yet), False if
        the key already existed — a replay, OR a second refresh racing the
        same token concurrently. This closes the check-then-add TOCTOU that
        a separate ``is_blacklisted()`` + ``add()`` leaves open (both racers
        pass the check before either writes). Shares the jti key space with
        ``add``/``is_blacklisted`` so a consumed refresh jti is also seen as
        blacklisted. Raises on Redis error so the caller fails CLOSED (503):
        we must not mint a rotated token when we cannot prove the old one
        was not already consumed.
        """
        r = _get_redis()
        key = f"{TokenBlacklist._KEY_PREFIX}{jti}"
        # redis-py returns True when the key was set, None when NX failed.
        result = await r.set(key, "1", nx=True, ex=expires_in_seconds)
        return result is not None

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
    async def revoke_all_for_user(user_id: str) -> datetime:
        """Store a revoke timestamp for the user, durably. Returns the exact UTC
        instant it stamped.

        Returning the instant lets a caller that mints a session in the SAME
        request (the break-glass redeem) wait until the wall clock ticks strictly
        past it before issuing the new token: is_revoked_by_user_logout_all
        compares ``issued_at <= revoke_time`` at whole-second precision and a
        future iat is rejected by the JWT decoder, so the new session must land in
        a LATER second to survive while every session up to this instant stays
        revoked. The timestamp is captured here, at the write, so nothing minted
        before the write can slip through a stale caller-side value.

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
        from datetime import UTC, datetime

        from sqlalchemy import update

        from src.db.models import User
        from src.db.session import async_engine

        # 1) Durable write to users.revoked_at — must succeed. The revoke instant
        # MUST come from the SAME clock that mints JWT `iat` (the API host's
        # time.time()/datetime.now), because is_revoked compares
        # `issued_at <= revoke_time`. Stamping it from the DB clock instead
        # (clock_timestamp) would introduce a cross-clock skew where an
        # API-ahead/DB-behind host leaves a just-issued token un-revoked (Codex).
        # So we use datetime.now(UTC), captured INSIDE the connection block right
        # before the UPDATE — after the pool wait — so the gap between capture and
        # write is just the statement round-trip (Codex P1: minimal window). The
        # residual sub-round-trip window matches the existing documented
        # "same-second login may need a retry" caveat on is_revoked.
        #
        # ACCEPTED RESIDUAL (Codex P2): if this UPDATE blocks on a row lock held
        # by a concurrent txn, the capture->write window grows by the lock wait.
        # We deliberately do NOT add a SELECT ... FOR UPDATE before the capture:
        # mfa_enable / mfa_disable call this function WHILE already holding a
        # with_for_update() lock on the same users row, so a second FOR UPDATE
        # here would risk a cross-transaction deadlock — worse than the narrow
        # race it would close. The robust fix is a monotonic token-version /
        # nonce revocation scheme (separate effort); timestamp precision is the
        # known limit of the current mechanism.
        async with async_engine.begin() as conn:
            now = datetime.now(UTC)
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
                # If both SETEX and DEL fail, the cache can retain a STALE
                # POSITIVE revoke timestamp (an earlier revoke) that
                # get_user_revoke_time trusts WITHOUT re-reading the DB —
                # masking THIS revocation and letting tokens issued after the
                # old timestamp survive. Fail CLOSED: re-raise so the caller
                # 503s and retries. The DB write above is durable + idempotent,
                # so a retry once Redis recovers makes the cache consistent.
                # (Codex convergence review.)
                _logger.error(
                    "revoke_all_for_user: BOTH Redis SETEX and DEL failed; "
                    "failing closed for user %s: %s",
                    user_id, del_exc,
                )
                raise

        return now

    @staticmethod
    async def update_revoke_cache(user_id: str, now: datetime) -> None:
        """Write ONLY the Redis revoke cache for an IN-SESSION revoke.

        For callers that already hold a `SELECT ... FOR UPDATE` lock on the users
        row (mfa_enable / mfa_disable): they stamp `users.revoked_at` on their OWN
        transaction/connection and commit it atomically with their change, then
        call this for the cache. They must NOT call revoke_all_for_user — its
        separate NullPool connection would block forever on the held row lock
        (the request coroutine is suspended awaiting that very UPDATE → app-level
        deadlock; Codex HIGH).

        Call this BEFORE the caller commits and 503 on failure: a cache that
        can't be written rolls the whole change back, leaving the account
        unchanged (fail-closed).

        ALWAYS raise on a SETEX failure (Codex). Unlike revoke_all_for_user — which
        commits the new users.revoked_at to the DB FIRST and only then touches
        Redis — this runs while the caller's new revoked_at is still UNCOMMITTED.
        So we must NOT treat a DEL as a success path: a cleared cache lets a
        concurrent token check fall through to the DB, read the OLD revoked_at,
        and backfill that stale positive value; after the caller commits the newer
        revoked_at the cache still serves the old one until TTL, letting tokens
        issued after the old revoke survive (fail-open). The DEL here is only
        best-effort cleanup; we re-raise regardless so the caller 503s and rolls
        back (revoked_at reverts to the old value, consistent with whatever the
        cache holds).
        """
        key = f"{TokenBlacklist._USER_REVOKE_PREFIX}{user_id}"
        try:
            r = _get_redis()
            await r.setex(
                key, TokenBlacklist._REVOKE_CACHE_TTL_SECONDS, str(int(now.timestamp())),
            )
        except redis_exceptions.RedisError as exc:
            _logger.error(
                "update_revoke_cache: SETEX failed; failing closed (best-effort "
                "DEL cleanup) for user %s: %s", user_id, exc,
            )
            try:
                await _get_redis().delete(key)
            except redis_exceptions.RedisError:
                pass
            raise

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


# Lua: atomically (1) increment the failure counter, (2) refresh its sliding
# TTL, (3) compute the progressive lock duration from the NEW count, and (4)
# write a SEPARATE lock key whose TTL only ever moves UP (monotonic). Doing all
# four in one script is what makes the lockout correct:
#   * The counter and the lock are now DISTINCT keys. The old code derived the
#     lockout straight from the counter, so a count stuck at 5 (the counter only
#     expires via its key TTL, it never decays below a threshold) kept check()
#     returning a lockout for the WHOLE counter TTL — 5 fat-fingered passwords
#     locked an IP for ~24h instead of the documented 1 min, and the progressive
#     1/5/30-min tiers were pure fiction (only the Retry-After header changed).
#   * Monotonic SET (only when the new duration exceeds the lock's remaining
#     TTL) closes the race Codex flagged: a slow low-count request can no longer
#     overwrite — and shorten — a lock a high-count request already set.
#   * INCR+EXPIRE+SET in one script means a crash/interleave can't leave the
#     counter bumped with no lock, nor a lock written from a stale count.
#   KEYS[1] = counter key       KEYS[2] = lock key
#   ARGV[1] = counter TTL (s)   ARGV[2] = lock cap (s, 0 = uncapped)
#   ARGV[3..] = threshold, duration pairs (ascending)
# Returns the new counter value.
_RECORD_FAILURE_LUA = """
local count = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
local cap = tonumber(ARGV[2])
local duration = 0
local i = 3
while i < #ARGV do
    if count >= tonumber(ARGV[i]) then
        duration = tonumber(ARGV[i + 1])
    end
    i = i + 2
end
if cap > 0 and duration > cap then
    duration = cap
end
if duration > 0 then
    local remaining = redis.call('TTL', KEYS[2])
    if remaining < 0 then
        remaining = 0
    end
    if duration > remaining then
        redis.call('SET', KEYS[2], '1', 'EX', duration)
    end
end
return count
"""


class BruteForceProtection:
    """Per-IP and per-email progressive lockout.

    Tracks failure counts independently for the client IP and the target
    email so that distributed attacks (many IPs, one email) are also caught.

    The failure COUNTER and the LOCK are deliberately separate keys (see
    _RECORD_FAILURE_LUA): record_failure() increments the counter and, when a
    threshold is crossed, arms a short-lived lock; check() consults ONLY the
    lock. That is what makes the progressive ladder real — the lowest tier now
    releases after 1 min instead of persisting for the counter's whole TTL.
    """

    _KEY_PREFIX = "bf:"
    # Lock keys live under a distinct prefix so they never collide with the
    # counter keys (bf:ip:… / bf:email:…) that drive escalation.
    _LOCK_PREFIX = "bf:lock:"

    # A6: the per-email counter exists to catch distributed attacks (many
    # IPs, one email). But an unauthenticated attacker who knows a victim's
    # email can spray bad passwords from throwaway IPs to drive that email
    # counter to the 24h tier and deny the real user login at will — a
    # cheap, repeatable account-lockout DoS. Cap email-driven lockout at a
    # short ceiling: it still slows distributed guessing, but cannot be
    # weaponised into a long denial of service. The per-IP counter keeps
    # the FULL escalation (incl. 24h) against the actual source of an
    # attack, where a long lockout is the desired outcome.
    _EMAIL_LOCKOUT_CAP_SECONDS = 15 * 60

    # Counter memory windows (sliding — refreshed on each failure). The IP
    # counter remembers for 24h so a persistent single source escalates to the
    # 24h tier; the email counter is kept short (Codex: a long email memory lets
    # a low-traffic attacker hold a victim near-permanently locked).
    _IP_COUNTER_TTL = 24 * 3600
    _EMAIL_COUNTER_TTL = _EMAIL_LOCKOUT_CAP_SECONDS

    @staticmethod
    def _lockout_duration(failures: int, *, is_email: bool = False) -> int:
        """Return lockout seconds for a given failure count (0 = not locked out).

        Pure mirror of the ladder the Lua script applies — used by the tests to
        assert expected durations. The production lock is computed INSIDE
        _RECORD_FAILURE_LUA so the compute+set is atomic; this stays the single
        readable definition of the ladder.
        """
        duration = 0
        for threshold, seconds in _LOCKOUT_THRESHOLDS:
            if failures >= threshold:
                duration = seconds
        if is_email:
            duration = min(duration, BruteForceProtection._EMAIL_LOCKOUT_CAP_SECONDS)
        return duration

    @staticmethod
    def _flat_thresholds() -> list[int]:
        """Flatten _LOCKOUT_THRESHOLDS into [th, sec, th, sec, …] for the Lua ARGV."""
        flat: list[int] = []
        for threshold, seconds in _LOCKOUT_THRESHOLDS:
            flat.extend((threshold, seconds))
        return flat

    @staticmethod
    async def check(ip: str, email: str) -> None:
        """Raise HTTP 429 if either the IP or email is currently locked out.

        Reads ONLY the lock keys (not the counters), so the lockout lasts
        exactly the duration record_failure() armed — not the counter's TTL.
        Retry-After is the lock's real remaining TTL.

        Fails OPEN on Redis errors. Brute-force lockout is best-effort
        defense layered on top of the password hash check; if Redis is
        unavailable we cannot tell whether the caller is locked out, but
        failing CLOSED would 500 every login attempt for the duration
        of the Redis incident — taking real users offline. Failing open
        re-exposes the attack surface for the outage window only, which
        is preferable to a full auth outage.
        """
        r = _get_redis()
        # H3: key on a FULL-LENGTH keyed HMAC of the email (blind_index, 64 hex),
        # never the plaintext — no enumerable address in a Redis key, and no
        # cross-account collision (the 12-char email_fingerprint is for LOGS only).
        # blind_index normalizes (casefold), so the bucket is case-insensitive. The
        # same key is used by record_failure/clear so the three stay consistent.
        ekey = blind_index(email)
        try:
            for key_suffix in [f"ip:{ip}", f"email:{ekey}"]:
                lock_key = f"{BruteForceProtection._LOCK_PREFIX}{key_suffix}"
                ttl = await r.ttl(lock_key)
                # A live lock key returns ttl >= 0 (we always SET … EX). Redis TTL
                # floors to whole seconds, so a key with under 1s left reports 0 —
                # treat ttl >= 0 (key still exists) as locked, NOT just ttl > 0, so
                # the final sub-second of a lock can't leak one early guess (Codex
                # P3). -2 (no key) and the never-written -1 are "not locked".
                if ttl is not None and ttl >= 0:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="Too many failed login attempts. Try again later.",
                        headers={"Retry-After": str(ttl if ttl > 0 else 1)},
                    )
        except redis_exceptions.RedisError as exc:
            _logger.warning(
                "BruteForceProtection.check fail-open: Redis error for ip=%s email_fp=%s: %s",
                ip, email_fingerprint(email), exc,
            )
            return

    _NOTIFY_THRESHOLD = 10  # Send email after this many failures

    @staticmethod
    async def record_failure(ip: str, email: str) -> None:
        """Increment the IP and email failure counters and arm the lock.

        Both the increment and the (monotonic) lock write happen atomically
        inside _RECORD_FAILURE_LUA — see that script for why counter and lock
        are separate keys. The IP path escalates fully (uncapped, 24h memory);
        the email path caps the lock at _EMAIL_LOCKOUT_CAP_SECONDS to stay a
        speed bump rather than a weaponisable account-lockout DoS.

        Sends a one-time lockout notification email when the email-based
        counter crosses the notification threshold. Best-effort: any
        Redis error is logged and swallowed so the outer login handler
        can still return its normal 401 response instead of 500-ing.
        """
        r = _get_redis()
        ekey = blind_index(email)  # H3: full-length keyed HMAC, never plaintext email
        thresholds = BruteForceProtection._flat_thresholds()
        email_failures = 0
        try:
            # IP: full escalation, 24h counter memory, uncapped lock (cap arg 0).
            await r.eval(
                _RECORD_FAILURE_LUA,
                2,
                f"{BruteForceProtection._KEY_PREFIX}ip:{ip}",
                f"{BruteForceProtection._LOCK_PREFIX}ip:{ip}",
                BruteForceProtection._IP_COUNTER_TTL,
                0,
                *thresholds,
            )
            # Email: capped lock + short counter memory (anti-DoS).
            email_failures = int(
                await r.eval(
                    _RECORD_FAILURE_LUA,
                    2,
                    f"{BruteForceProtection._KEY_PREFIX}email:{ekey}",
                    f"{BruteForceProtection._LOCK_PREFIX}email:{ekey}",
                    BruteForceProtection._EMAIL_COUNTER_TTL,
                    BruteForceProtection._EMAIL_LOCKOUT_CAP_SECONDS,
                    *thresholds,
                )
            )
        except redis_exceptions.RedisError as exc:
            _logger.warning(
                "BruteForceProtection.record_failure skipped (Redis error) ip=%s email_fp=%s: %s",
                ip, email_fingerprint(email), exc,
            )
            return

        # Send lockout notification once when threshold is first crossed.
        # Reachable from a single IP now: check() only blocks while the short
        # lock is live, so once it expires record_failure() runs again and the
        # email counter keeps climbing toward _NOTIFY_THRESHOLD.
        if email_failures == BruteForceProtection._NOTIFY_THRESHOLD:
            dedup_key = f"{BruteForceProtection._KEY_PREFIX}notified:{ekey}"
            try:
                already_sent = await r.get(dedup_key)
                if not already_sent:
                    await r.setex(dedup_key, 24 * 3600, "1")
                else:
                    return
            except redis_exceptions.RedisError as exc:
                _logger.warning(
                    "Lockout notification dedup check failed (Redis error) email_fp=%s: %s",
                    email_fingerprint(email), exc,
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
                        "Lockout notification failed for email_fp=%s: %s",
                        email_fingerprint(email), str(notify_exc)[:200],
                    )

    @staticmethod
    async def clear(ip: str, email: str) -> None:
        """Clear BOTH failure counters AND both lock keys on successful login.

        Must delete the lock keys too (Codex): otherwise a user who failed,
        got locked, then succeeded on the next allowed attempt would stay
        locked for the remainder of the armed lock TTL despite a valid login.

        Best-effort — if Redis is unavailable the stale keys just decay via
        their own TTLs. Failing the login because the keys could not be cleared
        would be a worse user experience.
        """
        r = _get_redis()
        ekey = blind_index(email)  # H3: must match check()/record_failure()
        try:
            await r.delete(
                f"{BruteForceProtection._KEY_PREFIX}ip:{ip}",
                f"{BruteForceProtection._KEY_PREFIX}email:{ekey}",
                f"{BruteForceProtection._LOCK_PREFIX}ip:{ip}",
                f"{BruteForceProtection._LOCK_PREFIX}email:{ekey}",
            )
        except redis_exceptions.RedisError as exc:
            _logger.warning(
                "BruteForceProtection.clear skipped (Redis error) ip=%s email_fp=%s: %s",
                ip, email_fingerprint(email), exc,
            )
