"""JWT + API key authentication and plan enforcement dependencies."""

import hashlib
import secrets
import time
import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from jwt.exceptions import InvalidTokenError as JWTError
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth_hardening import TokenBlacklist, constant_time_compare
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


def create_secure_token(user_id: str) -> str:
    """Create a signed access JWT (1 hour) with jti, iss, aud, and exp claims."""
    now = int(time.time())
    payload = {
        "sub": user_id,
        "jti": str(uuid.uuid4()),
        "iss": "bridgeleads",
        "aud": "bridgeleads-api",
        "iat": now,
        "exp": now + _ACCESS_TOKEN_EXPIRE_SECONDS,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=_ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    """Create a signed refresh JWT (7 days) for obtaining new access tokens."""
    now = int(time.time())
    payload = {
        "sub": user_id,
        "jti": str(uuid.uuid4()),
        "iss": "bridgeleads",
        "aud": "bridgeleads-api",
        "purpose": "refresh",
        "iat": now,
        "exp": now + _REFRESH_TOKEN_EXPIRE_SECONDS,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=_ALGORITHM)


def decode_secure_token(token: str) -> dict:
    """Decode and validate a JWT. Raises JWTError on failure."""
    return jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=[_ALGORITHM],
        audience="bridgeleads-api",
        issuer="bridgeleads",
        options={"verify_exp": True},
    )


# ─── FastAPI auth dependency ──────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)

_CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or expired credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """FastAPI dependency: resolves the current user from JWT bearer or API key.

    Accepts:
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
        return user_match

    # ── JWT path ──────────────────────────────────────────────────────────────
    try:
        payload = decode_secure_token(token)
    except JWTError:
        raise _CREDENTIALS_EXCEPTION

    user_id: str = payload.get("sub", "")
    jti: str = payload.get("jti", "")
    issued_at: int = payload.get("iat", 0)

    if not user_id:
        raise _CREDENTIALS_EXCEPTION

    # Check blacklist (individual token logout)
    if await TokenBlacklist.is_blacklisted(jti):
        raise _CREDENTIALS_EXCEPTION

    # Check user-level revocation (logout-all)
    revoke_time = await TokenBlacklist.get_user_revoke_time(user_id)
    if revoke_time > 0 and issued_at < revoke_time:
        raise _CREDENTIALS_EXCEPTION

    result = await db.execute(
        select(User).where(User.id == user_id, User.is_active)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise _CREDENTIALS_EXCEPTION

    return user


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


CurrentUser = Annotated[User, Depends(get_current_user)]
