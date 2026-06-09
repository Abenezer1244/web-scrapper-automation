"""JWT + API key authentication and plan enforcement dependencies."""

import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import InvalidTokenError as JWTError
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth_hardening import TokenBlacklist
from src.config import settings
from src.db import User, get_db

# ─── Password hashing ─────────────────────────────────────────────────────────

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ─── API key hashing ──────────────────────────────────────────────────────────

def generate_api_key() -> tuple[str, str]:
    """Generate a new API key. Returns (raw_key, hash_to_store)."""
    raw = "bl_" + secrets.token_urlsafe(40)
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    return raw, key_hash


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# ─── JWT ──────────────────────────────────────────────────────────────────────

_ALGORITHM = "HS256"
_ACCESS_TOKEN_EXPIRE_SECONDS = 3600  # 1 hour
_REFRESH_TOKEN_EXPIRE_SECONDS = 7 * 24 * 3600  # 7 days
_ISSUER = "bridgeleads"
# Access and refresh tokens carry DISTINCT audiences so a refresh token cannot
# authenticate a normal request: decode_secure_token (the auth path) pins the
# access audience, so a refresh token fails the JWT audience check. Belt: access
# tokens are tagged purpose="access" and get_current_user rejects
# purpose="refresh" — this neutralizes any LEGACY refresh token still carrying
# the old shared audience during its remaining lifetime.
_ACCESS_AUDIENCE = "bridgeleads-api"
_REFRESH_AUDIENCE = "bridgeleads-refresh"

# ─── H2-P5: authentication-method (amr) + auth_time claims ─────────────────────
# `amr` records HOW the session was authenticated so sensitive endpoints can
# require a stronger session (e.g. admin step-up needs "mfa"). `auth_time` is the
# epoch second the user last actually authenticated (login / mfa-login), used for
# step-up FRESHNESS (re-prove a 2nd factor within N minutes). These claims live
# in the signed JWT (unforgeable) but are still SANITIZED on the refresh path so
# a legacy/garbage value can't smuggle a privilege in — refresh never ADDS "mfa"
# (no silent escalation) and never DROPS it (no silent downgrade).
_VALID_AMR = ("pwd", "mfa", "break_glass")


def _sanitize_amr(raw: object) -> list[str]:
    """Normalize an amr claim to an ordered, de-duped subset of _VALID_AMR.

    Anything unrecognized (non-list, unknown tokens, legacy tokens with no amr)
    collapses to the baseline ["pwd"] — a token always proves at least a password
    step, never more than it legitimately carried.
    """
    if not isinstance(raw, list):
        return ["pwd"]
    seen: list[str] = []
    for item in raw:
        if isinstance(item, str) and item in _VALID_AMR and item not in seen:
            seen.append(item)
    return seen or ["pwd"]


def _coerce_auth_time(raw: object) -> int | None:
    """Return raw as an int epoch second, or None if it isn't a clean integer.

    STRICT: rejects bool (Python bool is a subclass of int, so a JSON `true`
    would otherwise coerce to 1), floats, and strings. A non-conforming
    auth_time is treated as "unknown freshness" (None) rather than silently
    accepted — the step-up dependency fails closed on a None/stale auth_time.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    return None


def create_secure_token(
    user_id: str,
    amr: list[str] | None = None,
    auth_time: int | None = None,
) -> str:
    """Create a signed access JWT (1 hour) with jti, iss, aud, amr, and exp.

    amr defaults to ["pwd"]; auth_time defaults to now. Both are sanitized so an
    issued token never carries an unknown auth-method token.
    """
    now = int(time.time())
    payload = {
        "sub": user_id,
        "jti": str(uuid.uuid4()),
        "iss": _ISSUER,
        "aud": _ACCESS_AUDIENCE,
        "purpose": "access",
        "amr": _sanitize_amr(amr),
        # None (the default for fresh logins/register) -> stamp now. A caller
        # that passes an explicit value (e.g. /auth/refresh propagating an
        # existing session) gets it preserved verbatim; bools/garbage are
        # rejected by _coerce_auth_time and fall back to now.
        "auth_time": (
            _coerce_auth_time(auth_time)
            if _coerce_auth_time(auth_time) is not None
            else now
        ),
        "iat": now,
        "exp": now + _ACCESS_TOKEN_EXPIRE_SECONDS,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=_ALGORITHM)


def create_refresh_token(
    user_id: str,
    amr: list[str] | None = None,
    auth_time: int | None = None,
) -> str:
    """Create a signed refresh JWT (7 days), usable ONLY at /auth/refresh.

    Scoped to the refresh audience so it cannot authenticate normal requests
    (decode_secure_token pins the access audience). Carries the same amr +
    auth_time as the access token so /auth/refresh can propagate session strength
    and freshness UNCHANGED into the rotated pair.
    """
    now = int(time.time())
    payload = {
        "sub": user_id,
        "jti": str(uuid.uuid4()),
        "iss": _ISSUER,
        "aud": _REFRESH_AUDIENCE,
        "purpose": "refresh",
        "amr": _sanitize_amr(amr),
        # None (the default for fresh logins/register) -> stamp now. A caller
        # that passes an explicit value (e.g. /auth/refresh propagating an
        # existing session) gets it preserved verbatim; bools/garbage are
        # rejected by _coerce_auth_time and fall back to now.
        "auth_time": (
            _coerce_auth_time(auth_time)
            if _coerce_auth_time(auth_time) is not None
            else now
        ),
        "iat": now,
        "exp": now + _REFRESH_TOKEN_EXPIRE_SECONDS,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=_ALGORITHM)


def decode_secure_token(token: str) -> dict:
    """Decode and validate an ACCESS JWT. Raises JWTError on failure.

    Pins the access audience, so a refresh token (refresh audience) is rejected.
    """
    return jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=[_ALGORITHM],
        audience=_ACCESS_AUDIENCE,
        issuer=_ISSUER,
        options={"verify_exp": True},
    )


def decode_refresh_token(token: str) -> dict:
    """Decode and validate a REFRESH JWT (for /auth/refresh). Raises JWTError.

    Pins the refresh audience + purpose so an access token cannot be exchanged
    for new tokens here, and the same token cannot authenticate elsewhere.
    """
    payload = jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=[_ALGORITHM],
        audience=_REFRESH_AUDIENCE,
        issuer=_ISSUER,
        options={"verify_exp": True},
    )
    if payload.get("purpose") != "refresh":
        raise JWTError("not a refresh token")
    return payload


# ─── FastAPI auth dependency ──────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)

_CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or expired credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


@dataclass(frozen=True)
class AuthContext:
    """Everything proven about the caller, decoded ONCE per request.

    auth_method is "jwt" (bearer access token) or "api_key". amr/auth_time/jti/
    payload are only meaningful for the JWT path: an API key is a long-lived
    static credential with no authentication-method or freshness signal, so it
    carries amr=[] and auth_time=None and can never satisfy an MFA step-up
    (H2-P5: API-key sessions always fail step-up).
    """
    user: User
    auth_method: str  # "jwt" | "api_key"
    amr: list[str]
    auth_time: int | None
    jti: str | None
    payload: dict | None


async def get_auth_context(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AuthContext:
    """FastAPI dependency: resolve + validate the caller into an AuthContext.

    Single decode point for the request (FastAPI caches this dependency), so the
    admin/step-up dependencies and get_current_user all share one decode rather
    than re-parsing the bearer. Accepts:
    - Authorization: Bearer <jwt>
    - Authorization: Bearer <api_key>  (api keys start with 'bl_')
    """
    if credentials is None:
        raise _CREDENTIALS_EXCEPTION

    token = credentials.credentials

    # ── API key path ──────────────────────────────────────────────────────────
    if token.startswith("bl_"):
        # H7 (full-SaaS review): look up the user by exact hash via
        # the ix_users_api_key_hash index instead of scanning every
        # user in Python. The previous "iterate all users with a
        # constant-time compare" loop was O(N) per authenticated
        # API call — which gets worse as the user base grows — and
        # was defending against a timing attack that doesn't exist:
        # SHA-256 hash comparison via the btree index is constant
        # time relative to the caller (Postgres compares hashes at
        # the page level, not character-by-character), and SHA-256
        # is non-reversible so an attacker cannot derive the input
        # key from timing the DB probe. Direct equality lookup is
        # the standard pattern.
        key_hash = hash_api_key(token)
        result = await db.execute(
            select(User).where(
                User.api_key_hash == key_hash,
                User.is_active,
            )
        )
        user_match = result.scalar_one_or_none()
        if user_match is None:
            raise _CREDENTIALS_EXCEPTION
        return AuthContext(
            user=user_match,
            auth_method="api_key",
            amr=[],
            auth_time=None,
            jti=None,
            payload=None,
        )

    # ── JWT path ──────────────────────────────────────────────────────────────
    try:
        payload = decode_secure_token(token)
    except JWTError:
        raise _CREDENTIALS_EXCEPTION

    # A refresh token must never authenticate a request. New refresh tokens are
    # already rejected above by the audience pin; this also catches any LEGACY
    # refresh token minted under the old shared audience, for its lifetime.
    if payload.get("purpose") == "refresh":
        raise _CREDENTIALS_EXCEPTION

    user_id: str = payload.get("sub", "")
    jti: str = payload.get("jti", "")
    issued_at: int = payload.get("iat", 0)

    if not user_id:
        raise _CREDENTIALS_EXCEPTION

    # Check blacklist (individual token logout) and user-level
    # revocation (logout-all). Both are SECURITY BOUNDARIES — if Redis
    # is unavailable we cannot prove the token has not been revoked,
    # so we MUST refuse to serve the request. Surface 503 (Service
    # Unavailable) so the client can distinguish "auth-infra is
    # temporarily down" from a normal 401 ("your credentials are
    # invalid") and avoid forcing a re-login on a transient outage.
    import redis.exceptions as _redis_exceptions

    from src.api.middleware.auth_hardening import revocation_unavailable_503
    try:
        if await TokenBlacklist.is_blacklisted(jti):
            raise _CREDENTIALS_EXCEPTION

        if await TokenBlacklist.is_revoked_by_user_logout_all(user_id, issued_at):
            raise _CREDENTIALS_EXCEPTION
    except _redis_exceptions.RedisError:
        raise revocation_unavailable_503()

    result = await db.execute(
        select(User).where(User.id == user_id, User.is_active)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise _CREDENTIALS_EXCEPTION

    return AuthContext(
        user=user,
        auth_method="jwt",
        amr=_sanitize_amr(payload.get("amr")),
        auth_time=_coerce_auth_time(payload.get("auth_time")),
        jti=jti or None,
        payload=payload,
    )


async def get_current_user(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> User:
    """Thin wrapper over get_auth_context — preserves every existing CurrentUser
    dependency unchanged while the request decodes the bearer exactly once."""
    return ctx.user


# ─── Plan enforcement ─────────────────────────────────────────────────────────

def require_plan(*plans: str):
    """Dependency factory: raises HTTP 403 if user's plan is not in the allowed list.

    Usage:
        @router.post("/scrapers", dependencies=[Depends(require_plan("pro", "business", "agency"))])
    """
    async def _check(user: Annotated[User, Depends(get_current_user)]) -> User:
        if user.plan not in plans:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This feature requires one of: {', '.join(plans)}",
            )
        return user
    return _check


# ─── H2-P5: admin MFA enforcement + step-up ───────────────────────────────────
# Two layered dependencies:
#   require_admin      — admin-only gate. Non-admins get 404 (hide the endpoint's
#                        existence, matching the prior inline is_admin checks). An
#                        admin who has NOT enrolled MFA gets 403 with the
#                        machine-readable code "admin_mfa_enrollment_required" so
#                        the frontend can route them to the enrollment flow
#                        (FORCE-ENROLL). The Settings/enroll page must NOT itself
#                        be gated behind an admin 403 — a no-MFA admin has to be
#                        able to reach enrollment.
#   require_admin_mfa  — step-up gate for SENSITIVE admin ops. On top of
#                        require_admin it demands a JWT session (not an API key,
#                        which carries no auth-method/freshness signal) whose amr
#                        includes "mfa" AND whose auth_time is fresh
#                        (< _ADMIN_STEP_UP_MAX_AGE_SECONDS). Otherwise 403
#                        "admin_mfa_step_up_required" so the frontend can prompt a
#                        re-authentication via /auth/login/mfa.
_ADMIN_STEP_UP_MAX_AGE_SECONDS = 15 * 60  # 15 minutes
# Tolerance for a slightly-future auth_time. Our own minter stamps now() under one
# clock, so this should never be exceeded in practice; a wildly-future auth_time
# would indicate a minting bug and must NOT read as "fresh forever" (Codex P3).
_ADMIN_STEP_UP_FUTURE_SKEW_SECONDS = 60


async def require_admin(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> AuthContext:
    """Admin-only gate (404 to non-admins, 403 enrollment-required to no-MFA admins)."""
    if not ctx.user.is_admin:
        # 404, never 403: don't confirm the admin endpoint exists to a non-admin.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not found",
        )
    if not ctx.user.mfa_enabled:
        # Admin exists but hasn't enrolled MFA — force enrollment before any admin
        # surface is usable. Distinct code so the UI can deep-link to enrollment.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin_mfa_enrollment_required",
        )
    return ctx


async def require_admin_mfa(
    ctx: Annotated[AuthContext, Depends(require_admin)],
) -> AuthContext:
    """Step-up gate for sensitive admin ops: fresh, MFA-backed JWT session.

    require_admin (the dependency above) has already proven admin + enrolled. This
    layer additionally requires the CURRENT session to be MFA-backed and recent.
    API-key sessions always fail here (amr=[], auth_time=None) — a static
    long-lived key is exactly what step-up exists to defeat.
    """
    if ctx.auth_method != "jwt" or "mfa" not in ctx.amr:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin_mfa_step_up_required",
        )
    # Freshness: auth_time must be present and within the step-up window. A None
    # auth_time (unknown freshness) or a stale/0-sentinel one fails closed.
    now = int(time.time())
    if ctx.auth_time is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin_mfa_step_up_required",
        )
    age = now - ctx.auth_time
    # Bound BOTH sides: too old (> window) is stale; meaningfully in the future
    # (< -skew) is a minting bug / tampered clock and must not be trusted as
    # perpetually fresh (Codex P3). A few seconds of jitter is tolerated.
    if age > _ADMIN_STEP_UP_MAX_AGE_SECONDS or age < -_ADMIN_STEP_UP_FUTURE_SKEW_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin_mfa_step_up_required",
        )
    return ctx


CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentAuth = Annotated[AuthContext, Depends(get_auth_context)]
RequireAdmin = Annotated[AuthContext, Depends(require_admin)]
RequireAdminMfa = Annotated[AuthContext, Depends(require_admin_mfa)]
