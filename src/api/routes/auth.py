"""Auth routes: register, login, me, logout, logout-all, api-key."""

import time
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import (
    CurrentUser,
    create_refresh_token,
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


@router.get("/config")
async def auth_config() -> dict:
    """Public endpoint: returns auth validation rules for frontend forms.

    Keeps frontend placeholder text in sync with backend validation.
    """
    return {
        "password": {
            "min_length": 10,
            "max_length": 72,
            "placeholder": "Min. 10 characters",
        },
        "trial": {
            "days": 7,
            "plan": "pro",
            "records_limit": 500,
        },
    }


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
    refresh = create_refresh_token(user.id)
    audit_log(request, "register", user.id)

    # Send welcome email (non-blocking — failure must not break registration)
    try:
        from src.workers.onboarding_emails import send_welcome_email
        send_welcome_email(body.email)
    except Exception:
        pass

    return TokenResponse(access_token=token, refresh_token=refresh)


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
    refresh = create_refresh_token(user.id)
    audit_log(request, "login_success", user.id)
    return TokenResponse(access_token=token, refresh_token=refresh)


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    body: RefreshRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Exchange a valid refresh token for a new access + refresh token pair."""
    await rate_limit(request, zone="auth")
    from jwt.exceptions import InvalidTokenError
    try:
        payload = decode_secure_token(body.refresh_token)
    except (InvalidTokenError, Exception):
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    if payload.get("purpose") != "refresh":
        raise HTTPException(status_code=401, detail="Not a refresh token")

    user_id = payload.get("sub")
    result = await db.execute(select(User).where(User.id == user_id, User.is_active))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    # Check if the refresh token's jti was blacklisted (logout-all)
    from src.api.middleware.auth_hardening import TokenBlacklist
    jti = payload.get("jti", "")
    if jti and await TokenBlacklist.is_blacklisted(jti):
        raise HTTPException(status_code=401, detail="Refresh token revoked")

    new_access = create_secure_token(user.id)
    new_refresh = create_refresh_token(user.id)
    return TokenResponse(access_token=new_access, refresh_token=new_refresh)


@router.get("/me", response_model=UserResponse)
async def me(current_user: CurrentUser) -> UserResponse:
    return UserResponse.model_validate(current_user)


@router.get("/onboarding")
async def onboarding_status(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return the user's onboarding progress and next suggested action.

    The frontend uses this to show a getting-started wizard or checklist.
    """
    from src.db.models import Job, ScraperConfig

    # Check what the user has done
    configs_result = await db.execute(
        select(ScraperConfig).where(ScraperConfig.user_id == current_user.id)
    )
    configs = configs_result.scalars().all()

    jobs_result = await db.execute(
        select(Job).where(Job.user_id == current_user.id)
    )
    jobs = jobs_result.scalars().all()

    done_jobs = [j for j in jobs if j.status == "done"]

    steps = {
        "account_created": True,
        "scraper_configured": len(configs) > 0,
        "first_scrape_run": len(jobs) > 0,
        "first_scrape_completed": len(done_jobs) > 0,
        "first_export_downloaded": any(j.export_key for j in done_jobs),
    }

    completed = sum(1 for v in steps.values() if v)
    total = len(steps)

    # Determine next action
    if not steps["scraper_configured"]:
        next_action = {
            "action": "create_scraper",
            "title": "Set up your first scraper",
            "description": "Choose a county and record type to start pulling leads.",
            "cta": "New Scraper",
            "route": "/dashboard/scrapers/new",
        }
    elif not steps["first_scrape_run"]:
        config = configs[0]
        next_action = {
            "action": "run_scrape",
            "title": f"Run your first scrape on {config.county.title()}, {config.state.upper()}",
            "description": "Click 'Run Now' to start pulling records from the county portal.",
            "cta": "Run Now",
            "route": f"/dashboard/scrapers/{config.id}",
        }
    elif not steps["first_scrape_completed"]:
        next_action = {
            "action": "wait_for_scrape",
            "title": "Your scrape is running",
            "description": "Records are being pulled from the county portal. This usually takes 2-5 minutes.",
            "cta": "View Progress",
            "route": "/dashboard",
        }
    elif not steps["first_export_downloaded"]:
        job = done_jobs[0]
        next_action = {
            "action": "download_export",
            "title": f"Download your {job.record_count or 0} leads",
            "description": "Your records are ready. Download the CSV and start mailing today.",
            "cta": "Download CSV",
            "route": f"/dashboard/jobs/{job.id}",
        }
    else:
        next_action = {
            "action": "complete",
            "title": "You're all set!",
            "description": "Set up a daily schedule to get fresh leads automatically, or add more counties.",
            "cta": "Add Another County",
            "route": "/dashboard/scrapers/new",
        }

    return {
        "steps": steps,
        "completed": completed,
        "total": total,
        "progress_pct": int(completed / total * 100),
        "next_action": next_action,
        "trial_days_remaining": current_user.trial_ends_at and max(0, (current_user.trial_ends_at.replace(tzinfo=None) - __import__("datetime").datetime.utcnow()).days) if current_user.trial_ends_at else None,
    }


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

    # Check password history — reject reuse of last 5 passwords
    from src.db.models import PasswordHistory
    history_result = await db.execute(
        select(PasswordHistory)
        .where(PasswordHistory.user_id == current_user.id)
        .order_by(PasswordHistory.created_at.desc())
        .limit(5)
    )
    recent_hashes = history_result.scalars().all()

    # Also check against the current password
    if verify_password(body.new_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password cannot be the same as your current password.",
        )

    for entry in recent_hashes:
        if verify_password(body.new_password, entry.password_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="New password cannot be one of your last 5 passwords.",
            )

    # Save current password to history before changing
    db.add(PasswordHistory(user_id=current_user.id, password_hash=user.password_hash))

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
