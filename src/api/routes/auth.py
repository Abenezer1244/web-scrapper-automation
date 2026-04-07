"""Auth routes: register, login, me, logout, logout-all, api-key."""

import time
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import (
    CurrentUser,
    create_secure_token,
    decode_secure_token,
    generate_api_key,
    hash_password,
    require_plan,
    verify_password,
)
from src.api.middleware import BruteForceProtection, audit_log, client_ip, rate_limit
from src.api.schemas import ApiKeyResponse, PasswordChange, TokenResponse, UserLogin, UserRegister, UserResponse
from src.config import settings
from src.db import User, get_db  # noqa: F401 (User used in Annotated type)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: UserRegister,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    await rate_limit(request, zone="auth")

    # Check for duplicate — but return generic error (no user enumeration)
    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Registration failed. Please try again.",
        )

    from datetime import UTC, datetime, timedelta

    trial_end = datetime.now(UTC) + timedelta(days=7)

    user = User(
        id=str(uuid.uuid4()),
        email=body.email,
        password_hash=hash_password(body.password),
        plan="pro",
        records_limit=settings.PLAN_LIMITS["pro"],  # 500 records during trial
        trial_ends_at=trial_end,
    )
    db.add(user)
    await db.flush()

    token = create_secure_token(user.id)
    audit_log(request, "register", user.id)
    return TokenResponse(access_token=token)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: UserLogin,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    await rate_limit(request, zone="auth")

    ip = client_ip(request)
    await BruteForceProtection.check(ip, body.email)

    result = await db.execute(select(User).where(User.email == body.email, User.is_active))
    user = result.scalar_one_or_none()

    # Always run verify_password — even when user not found — to prevent timing attacks.
    # Use a static hash for the "user not found" case so timing is identical.
    _DUMMY_HASH = "$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW"
    password_ok = verify_password(body.password, user.password_hash if user else _DUMMY_HASH)

    if not user or not password_ok:
        await BruteForceProtection.record_failure(ip, body.email)
        audit_log(request, "login_failure", detail=f"email={body.email}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    await BruteForceProtection.clear(ip, body.email)
    token = create_secure_token(user.id)
    audit_log(request, "login_success", user.id)
    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserResponse)
async def me(current_user: CurrentUser) -> UserResponse:
    return UserResponse.model_validate(current_user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    current_user: CurrentUser,
) -> None:
    # Extract raw token from Authorization header
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.removeprefix("Bearer ").strip()

    try:
        payload = decode_secure_token(token)
        jti = payload.get("jti", "")
        exp = payload.get("exp", 0)
        ttl = max(0, exp - int(time.time()))
        if jti and ttl > 0:
            from src.api.middleware.auth_hardening import TokenBlacklist
            await TokenBlacklist.add(jti, ttl)
    except Exception:
        pass  # Token already invalid — that's fine

    audit_log(request, "logout", current_user.id)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    request: Request,
    current_user: CurrentUser,
) -> None:
    from src.api.middleware.auth_hardening import TokenBlacklist
    await TokenBlacklist.revoke_all_for_user(current_user.id)
    audit_log(request, "logout_all", current_user.id)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChange,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Change the current user's password."""
    await rate_limit(request, zone="auth")

    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one()

    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect.",
        )

    user.password_hash = hash_password(body.new_password)
    await db.commit()
    audit_log(request, "password_changed", current_user.id)


@router.post("/api-key", response_model=ApiKeyResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    request: Request,
    current_user: Annotated[User, Depends(require_plan("business", "agency"))],
    db: AsyncSession = Depends(get_db),
) -> ApiKeyResponse:
    """Generate a new API key. The raw key is shown exactly once."""
    raw_key, key_hash = generate_api_key()

    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one()
    user.api_key_hash = key_hash
    await db.flush()

    audit_log(request, "api_key_created", current_user.id)
    return ApiKeyResponse(api_key=raw_key)
