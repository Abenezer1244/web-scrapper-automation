"""Body logic for the login family: POST /auth/login, /auth/login/mfa,
/auth/login/break-glass, /auth/refresh. Extracted VERBATIM from auth.py — the
route decorators + signatures stay in auth.py; these hold the moved bodies.
"""

import json
import time
from datetime import UTC, datetime

from fastapi import HTTPException, Request, status
from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import (
    _coerce_auth_time,
    _sanitize_amr,
    create_refresh_token,
    create_secure_token,
    decode_refresh_token,
    verify_password,
)
from src.api.middleware import BruteForceProtection, audit_log, client_ip, rate_limit
from src.api.schemas import (
    BreakGlassLoginRequest,
    LoginResponse,
    MfaLoginRequest,
    TokenResponse,
    UserLogin,
)
from src.db import User
from src.utils.crypto import blind_index
from src.utils.logger import email_fingerprint

from .tokens import (
    _MFA_CHALLENGE_EXPIRE_SECONDS,
    _consume_second_factor,
    _decode_mfa_challenge_token,
    _mint_mfa_challenge_token,
)


async def login_user(
    body: UserLogin,
    request: Request,
    db: AsyncSession,
) -> LoginResponse:
    await rate_limit(request, zone="auth")

    ip = client_ip(request)
    await BruteForceProtection.check(ip, body.email)

    # H3: match by the email blind index (email is encrypted at rest).
    result = await db.execute(
        select(User).where(User.email_hmac == blind_index(body.email), User.is_active)
    )
    user = result.scalar_one_or_none()

    # Always run verify_password — even when user not found — to prevent timing attacks.
    # Use a static hash for the "user not found" case so timing is identical.
    _DUMMY_HASH = "$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW"
    password_ok = verify_password(body.password, user.password_hash if user else _DUMMY_HASH)

    if not user or not password_ok:
        await BruteForceProtection.record_failure(ip, body.email)
        # PII (M2): log a stable email FINGERPRINT, never the plaintext address.
        # Lets ops correlate repeated attempts on the same account (same fp)
        # without an enumerable email in the audit log. HMAC-keyed (not a bare
        # hash) so the logs can't be brute-forced against a known email list.
        audit_log(request, "login_failure", detail=f"email_fp={email_fingerprint(body.email)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    # Password is correct. If MFA is enabled, login is NOT complete: issue a
    # short-lived challenge token and require a 2nd factor at /auth/login/mfa.
    # We deliberately do NOT clear the brute-force counter here — a password-only
    # success is not a completed login (Codex H2-P3), and we do NOT issue any
    # access/refresh token in this branch.
    if user.mfa_enabled:
        # Per-user limiter (not just per-IP): caps an attacker who already knows
        # the password from farming challenge tokens across rotating IPs. This
        # is a SEPARATE bucket from the verify limiter (mfa-verify:) so that
        # challenge-issuance traffic cannot exhaust the real user's ability to
        # submit a code (Codex H2-P3 review).
        await rate_limit(request, zone="auth", identifier=f"mfa-issue:{user.id}")
        audit_log(request, "mfa_challenge", user.id)
        return LoginResponse(
            mfa_required=True,
            mfa_token=_mint_mfa_challenge_token(user.id),
        )

    await BruteForceProtection.clear(ip, body.email)
    # Password-only session (no MFA on this account): amr=["pwd"].
    token = create_secure_token(user.id, amr=["pwd"])
    refresh = create_refresh_token(user.id, amr=["pwd"])
    audit_log(request, "login_success", user.id)
    return LoginResponse(access_token=token, refresh_token=refresh)


async def login_mfa_redeem(
    body: MfaLoginRequest,
    request: Request,
    db: AsyncSession,
) -> LoginResponse:
    await rate_limit(request, zone="auth")
    import jwt
    import redis.exceptions as _redis_exceptions

    from src.api.middleware.auth_hardening import (
        TokenBlacklist,
        revocation_unavailable_503,
    )

    try:
        payload = _decode_mfa_challenge_token(body.mfa_token)
    except jwt.InvalidTokenError:
        # Expired / wrong-audience / wrong-purpose / tampered → generic 401.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired MFA challenge.",
        )
    user_id = payload.get("sub")
    jti = payload.get("jti", "")
    issued_at = payload.get("iat", 0)

    # Per-user VERIFY limiter caps 2nd-factor guessing across rotating IPs. A bad
    # code is deliberately NOT fed into the password (ip,email) brute-force
    # bucket: that would let a password-knowing attacker DoS-lock the real user
    # and would conflate password compromise with MFA guessing. Kept separate
    # from the mfa-issue: bucket so challenge farming can't exhaust it (Codex).
    await rate_limit(request, zone="auth", identifier=f"mfa-verify:{user_id}")

    # The challenge subject was cryptographically proven (correct password) at
    # /auth/login, so we can safely bind the RLS context to it BEFORE any query.
    # This makes the User lookup and the backup-code consume correct under a
    # future RLS-enforce cutover (H1) — where an unset app.current_user_id would
    # otherwise match zero rows — without depending on a runtime RLS bypass
    # (Codex H2-P3). Harmless while the role still bypasses RLS.
    db.sync_session.info["rls_user_id"] = str(user_id)
    await db.execute(
        text("SELECT set_config('app.current_user_id', :uid, true)"),
        {"uid": str(user_id)},
    )

    # Revocation gate (P1): a challenge minted BEFORE a password change /
    # password reset / logout-all must not be redeemable, even inside its 5-min
    # window. Mirrors get_current_user — fail CLOSED (503) if Redis is down so we
    # never issue a session we can't prove is un-revoked.
    try:
        if await TokenBlacklist.is_revoked_by_user_logout_all(user_id, issued_at):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired MFA challenge.",
            )
    except _redis_exceptions.RedisError:
        raise revocation_unavailable_503()

    result = await db.execute(select(User).where(User.id == user_id, User.is_active))
    user = result.scalar_one_or_none()
    if user is None or not user.mfa_enabled:
        # Token valid but the user vanished / disabled MFA in the meantime.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired MFA challenge.",
        )

    if not await _consume_second_factor(db, user, body.code):
        # Nothing was consumed (0 rows matched); the session rolls back on raise.
        audit_log(request, "mfa_failure", user.id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication code.",
        )

    # Replay gate (P2): burn the challenge jti exactly once so a stolen/phished
    # challenge + one valid code cannot mint repeated sessions within the window.
    # consume_once is atomic (Redis SET NX) and raises on Redis error → 503
    # (fail closed). A wrong code above never reaches here, so a single typo does
    # not burn the challenge — retries (rate-limited) are still allowed.
    try:
        claimed = await TokenBlacklist.consume_once(jti, _MFA_CHALLENGE_EXPIRE_SECONDS)
    except _redis_exceptions.RedisError:
        raise revocation_unavailable_503()
    if not claimed:
        # Challenge already redeemed (replay or a concurrent winner).
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired MFA challenge.",
        )

    # 2nd factor good and challenge claimed — commit any backup-code consumption,
    # then complete login.
    await db.commit()
    await BruteForceProtection.clear(client_ip(request), user.email)
    # Full MFA-backed session (H2-P5): amr=["pwd","mfa"], auth_time=now (default)
    # — this is the only login path that mints an "mfa" session, the marker the
    # admin step-up dependency requires.
    token = create_secure_token(user.id, amr=["pwd", "mfa"])
    refresh = create_refresh_token(user.id, amr=["pwd", "mfa"])
    audit_log(request, "login_success", user.id)
    return LoginResponse(access_token=token, refresh_token=refresh)


async def login_break_glass_redeem(
    body: BreakGlassLoginRequest,
    request: Request,
    db: AsyncSession,
) -> LoginResponse:
    # Coarse IP limiter BEFORE we trust any identity from the token (Codex).
    await rate_limit(request, zone="auth")
    import jwt
    import redis.exceptions as _redis_exceptions

    from src.api.middleware.auth_hardening import (
        TokenBlacklist,
        revocation_unavailable_503,
    )
    from src.db.models import MfaBackupCode, MfaBreakGlassCode
    from src.utils.mfa import hash_backup_code

    try:
        payload = _decode_mfa_challenge_token(body.mfa_token)
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired MFA challenge.",
        )
    user_id = payload.get("sub")
    jti = payload.get("jti", "")
    issued_at = payload.get("iat", 0)

    # Per-user break-glass verify bucket — separate from mfa-verify/mfa-issue so
    # the buckets can't starve each other; caps code-guessing across IPs.
    await rate_limit(request, zone="auth", identifier=f"mfa-breakglass:{user_id}")

    # Bind RLS to the cryptographically-proven challenge subject before any query.
    db.sync_session.info["rls_user_id"] = str(user_id)
    await db.execute(
        text("SELECT set_config('app.current_user_id', :uid, true)"),
        {"uid": str(user_id)},
    )

    # Revocation gate: a challenge minted before a logout-all / password change is
    # dead even inside its 5-min window. Fail-closed 503 if Redis is down.
    try:
        if await TokenBlacklist.is_revoked_by_user_logout_all(user_id, issued_at):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired MFA challenge.",
            )
    except _redis_exceptions.RedisError:
        raise revocation_unavailable_503()

    result = await db.execute(select(User).where(User.id == user_id, User.is_active))
    user = result.scalar_one_or_none()
    if user is None or not user.mfa_enabled:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired MFA challenge.",
        )

    now = datetime.now(UTC)
    ua = (request.headers.get("user-agent") or "")[:500]
    ip = client_ip(request)

    # ATOMIC single-use consume: burn exactly one unused, unrevoked, unexpired
    # break-glass code for this user. RETURNING is non-empty iff one matched. This
    # is the GATE — nothing destructive happens until a code is proven valid, so a
    # password-knowing attacker cannot trigger a recovery/revocation with a bogus
    # code. Staged (uncommitted) so a later failure rolls the consume back.
    code_hash = hash_backup_code((body.code or "").strip())
    consumed = await db.execute(
        update(MfaBreakGlassCode)
        .where(
            MfaBreakGlassCode.user_id == user_id,
            MfaBreakGlassCode.code_hash == code_hash,
            MfaBreakGlassCode.used_at.is_(None),
            MfaBreakGlassCode.revoked_at.is_(None),
            or_(
                MfaBreakGlassCode.expires_at.is_(None),
                MfaBreakGlassCode.expires_at > now,
            ),
        )
        .values(used_at=now, used_ip=ip, used_user_agent=ua)
        .returning(MfaBreakGlassCode.id)
    )
    if consumed.scalar_one_or_none() is None:
        # Generic failure — never distinguish invalid vs used vs expired vs revoked.
        audit_log(request, "mfa_breakglass_failure", user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid recovery code.",
        )

    # Burn the challenge jti so a stolen challenge + one valid code can't mint
    # repeated recoveries within the window. A wrong code never reaches here.
    try:
        claimed = await TokenBlacklist.consume_once(jti, _MFA_CHALLENGE_EXPIRE_SECONDS)
    except _redis_exceptions.RedisError:
        raise revocation_unavailable_503()
    if not claimed:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired MFA challenge.",
        )

    # FAIL-SAFE ORDERING (mirrors change_password / reset_user_mfa): revoke ALL
    # sessions BEFORE committing the recovery. Our session currently holds only
    # the break_glass row lock (from the consume above) — NOT the users row — so
    # revoke_all_for_user's own transaction does not contend with us (Codex: no
    # deadlock). 503 on Redis: the staged consume rolls back (code reusable) and
    # nothing is torn down — safe.
    #
    # is_revoked uses issued_at <= revoke_time at whole-second precision, so
    # revoking at the current instant catches EVERY pre-existing session,
    # including any minted earlier in this same second. revoke_all_for_user
    # stamps the time AT its write and RETURNS it (so there is no stale
    # caller-side timestamp a racing token could slip past). The recovery session
    # we mint below must then land in a LATER second to survive (a future iat is
    # rejected by the decoder), so after the commit we wait for the wall clock to
    # tick strictly past the returned revoke instant before minting.
    try:
        revoke_at = await TokenBlacklist.revoke_all_for_user(user_id)
    except _redis_exceptions.RedisError:
        raise revocation_unavailable_503()

    # RECOVERY effect, committed atomically with the consume. Targeted Core
    # UPDATE so we never clobber the revoked_at that revoke_all_for_user just
    # wrote. Tear MFA down to un-enrolled + burn every remaining recovery
    # credential so one redemption invalidates the whole set.
    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            mfa_enabled=False,
            mfa_secret_encrypted=None,
            mfa_last_totp_counter=None,
            mfa_enrolled_at=None,
            api_key_hash=None,
        )
    )
    await db.execute(delete(MfaBackupCode).where(MfaBackupCode.user_id == user_id))
    await db.execute(
        update(MfaBreakGlassCode)
        .where(
            MfaBreakGlassCode.user_id == user_id,
            MfaBreakGlassCode.used_at.is_(None),
            MfaBreakGlassCode.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    await db.commit()

    # Wait for the wall clock to advance strictly past the revoke second, so the
    # recovery session minted next has iat > revoke_time and survives the
    # issued_at <= revoke_time check (while every same-second-or-earlier session
    # stays revoked). Bounded (~1s worst case, the time to the next second tick)
    # with a hard cap so a misbehaving clock can never hang the request.
    import asyncio

    revoke_ts = int(revoke_at.timestamp())
    for _ in range(100):  # cap ~2s
        if int(time.time()) > revoke_ts:
            break
        await asyncio.sleep(0.02)
    else:
        # The clock never advanced past the revoke second within the cap (clock
        # skew / frozen time). Minting now would hand back a token whose iat could
        # be <= revoke_ts — i.e. a SELF-REVOKED session. Fail closed instead: the
        # recovery (MFA cleared, sessions revoked) is already committed and
        # durable, so the user simply re-logs in (MFA is now off) to get a clean
        # session. (Codex P3.)
        raise revocation_unavailable_503()

    # Mint the DEGRADED recovery session. amr WITHOUT "mfa" -> can never satisfy
    # admin step-up; mfa_enabled is now False so require_admin routes the user to
    # re-enrollment.
    token = create_secure_token(user.id, amr=["pwd", "break_glass"])
    refresh = create_refresh_token(user.id, amr=["pwd", "break_glass"])
    audit_log(request, "mfa_breakglass_used", user_id)
    return LoginResponse(access_token=token, refresh_token=refresh)


# How long a racer waits for the winner to publish its rotation result before
# concluding the token is genuinely a replay. Only ever paid on the loser of a
# concurrent refresh, never on the normal path.
_ROTATION_PUBLISH_WAIT_SECONDS = 1.5
_ROTATION_POLL_INTERVAL_SECONDS = 0.05


async def _await_rotation_result(jti: str) -> str | None:
    """Poll for the rotation result the winner of this jti is about to publish.

    Returns the cached pair as soon as it appears, or None once the wait lapses —
    at which point the token really is a replay from outside the grace window
    (or the winner died before publishing), and the caller rejects it.
    """
    import asyncio

    from src.api.middleware.auth_hardening import TokenBlacklist

    deadline = time.monotonic() + _ROTATION_PUBLISH_WAIT_SECONDS
    while True:
        cached = await TokenBlacklist.recall_rotation(jti)
        if cached:
            return cached
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(_ROTATION_POLL_INTERVAL_SECONDS)


async def refresh_tokens(
    body,
    request: Request,
    db: AsyncSession,
) -> TokenResponse:
    await rate_limit(request, zone="auth")
    from jwt.exceptions import InvalidTokenError
    try:
        payload = decode_refresh_token(body.refresh_token)
    except InvalidTokenError:
        # A5: catch ONLY the JWT error. `decode_secure_token` performs no
        # infra I/O, so an operational error (e.g. a SECRET_KEY rotation
        # mishap) must surface as a 500 — not be silently masked as a 401
        # "invalid token", which would hide the misconfiguration and erase
        # the distinction between "attacker sent garbage" and "our config
        # is broken".
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    if payload.get("purpose") != "refresh":
        raise HTTPException(status_code=401, detail="Not a refresh token")

    user_id = payload.get("sub")
    result = await db.execute(select(User).where(User.id == user_id, User.is_active))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    # Refresh-token revocation + single-use rotation (A1).
    #   1. logout-all (user-level): any refresh token issued at/before the
    #      user's revoke timestamp is rejected — an attacker holding a stale
    #      refresh token cannot mint fresh access tokens after the user
    #      invalidated all sessions.
    #   2. A1 + race fix: ATOMICALLY consume this token's jti via SET NX.
    #      The NX succeeds exactly once, so a replay — OR a second /refresh
    #      racing the SAME token concurrently (the TOCTOU that a separate
    #      is_blacklisted()+add() leaves open: both racers pass the check
    #      before either writes) — loses the race and is rejected. This both
    #      rotates (single-use refresh tokens) and closes the race in one
    #      step. Once the victim's client refreshes, the attacker's stolen
    #      copy 401s, killing the session and surfacing the theft.
    # Revocation is a security boundary — if Redis is unavailable we cannot
    # prove the token wasn't revoked or already used, so surface 503.
    import redis.exceptions as _redis_exceptions

    from src.api.middleware.auth_hardening import TokenBlacklist, revocation_unavailable_503
    jti = payload.get("jti", "")
    issued_at: int = payload.get("iat", 0)
    exp: int = payload.get("exp", 0)
    ttl = max(0, exp - int(time.time()))
    if not jti or ttl <= 0:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    try:
        if await TokenBlacklist.is_revoked_by_user_logout_all(user_id, issued_at):
            raise HTTPException(status_code=401, detail="Refresh token revoked")
        if not await TokenBlacklist.consume_once(jti, ttl):
            # Already consumed. Inside the grace window this is almost always a
            # benign client race (parallel requests presenting the same token
            # before the first rotation's cookie propagated), not theft — so
            # hand back the SAME pair that consumption produced rather than
            # killing a healthy session. Outside the window it still 401s, which
            # is where a genuinely stolen token surfaces.
            #
            # Wait briefly rather than asking once. The winner claims the jti
            # BEFORE it mints the new pair, so between its SET NX and its
            # remember_rotation there is a real gap — a user lookup plus two JWT
            # signings. A racer that lands inside that gap would read an empty
            # cache and get the very 401 this window exists to prevent, which is
            # exactly the concurrent case we are fixing.
            replayed = await _await_rotation_result(jti)
            if replayed:
                cached = json.loads(replayed)
                return TokenResponse(
                    access_token=cached["access_token"],
                    refresh_token=cached["refresh_token"],
                )
            raise HTTPException(status_code=401, detail="Refresh token already used")
    except _redis_exceptions.RedisError:
        raise revocation_unavailable_503()

    # H2-P5: carry session strength + freshness across rotation UNCHANGED.
    #   - amr is sanitized (subset of {pwd,mfa,break_glass}; legacy tokens with
    #     no amr collapse to ["pwd"]). Refresh NEVER adds "mfa" (no silent
    #     escalation to a step-up-capable session without a real 2nd factor) and
    #     NEVER drops it (no silent downgrade of a legitimate MFA session).
    #   - auth_time is copied as-is so refresh does NOT reset step-up freshness;
    #     a stale MFA session must re-run /auth/login/mfa, not just /refresh, to
    #     pass step-up. Legacy tokens with no auth_time fall back to now() in the
    #     minter, but they also lack "mfa" in amr so step-up still fails them.
    propagated_amr = _sanitize_amr(payload.get("amr"))
    # FAIL-CLOSED freshness (Codex P1): copy a real auth_time verbatim, but if it
    # is missing/garbage on this refresh token, substitute 0 (the epoch — always
    # stale) rather than letting the minter stamp "now". Without this an
    # amr=["pwd","mfa"] refresh token with no/invalid auth_time would rotate into
    # a FRESH MFA-capable session and silently pass step-up freshness; with it,
    # such a token still keeps its "mfa" strength but is forced through a real
    # re-auth at /auth/login/mfa before it can clear step-up.
    propagated_auth_time = _coerce_auth_time(payload.get("auth_time"))
    if propagated_auth_time is None:
        propagated_auth_time = 0
    new_access = create_secure_token(
        user.id, amr=propagated_amr, auth_time=propagated_auth_time
    )
    new_refresh = create_refresh_token(
        user.id, amr=propagated_amr, auth_time=propagated_auth_time
    )
    # Record what this jti bought, so a replay racing inside the grace window
    # gets the same pair instead of a 401 that would end a healthy session.
    # Best-effort: the rotation itself already succeeded, and a Redis hiccup here
    # must not fail a request that is otherwise complete — it only means a
    # concurrent racer falls back to the strict "already used" answer.
    try:
        await TokenBlacklist.remember_rotation(
            jti,
            json.dumps({"access_token": new_access, "refresh_token": new_refresh}),
        )
    except _redis_exceptions.RedisError:
        pass
    return TokenResponse(access_token=new_access, refresh_token=new_refresh)
