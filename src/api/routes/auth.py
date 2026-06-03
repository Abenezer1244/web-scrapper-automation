"""Auth routes: register, login, me, logout, logout-all, api-key."""

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select, text
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
from src.api.schemas import (
    ApiKeyResponse,
    ForgotPasswordRequest,
    NotificationPrefsUpdate,
    PasswordChange,
    ResetPasswordRequest,
    TokenResponse,
    UserLogin,
    UserRegister,
    UserResponse,
)
from src.api.deps import get_rls_db
from src.config import settings
from src.db import User, get_db  # noqa: F401 (User used in Annotated type)

router = APIRouter(prefix="/auth", tags=["auth"])


# ─── A3: password-reset token (stateless) ─────────────────────────────────────
# A3 finding: there is no forgot/reset-password flow, so a user whose password
# is compromised has no recovery path. We reuse the existing JWT/HS256 machinery
# (same SECRET_KEY as src/api/auth.py) rather than add a DB table, but mint reset
# tokens under a DISTINCT audience so the two token families cannot cross over:
#   - a session/access token (aud="bridgeleads-api") CANNOT reset a password
#     (verify below pins audience="bridgeleads-reset"), and
#   - a reset token CANNOT authenticate a request (get_current_user's
#     decode_secure_token pins audience="bridgeleads-api").
# We deliberately do NOT reuse decode_secure_token here because it hard-codes
# aud="bridgeleads-api"; we call jwt.* directly with the reset audience.

_RESET_TOKEN_PURPOSE = "reset"
_RESET_TOKEN_AUDIENCE = "bridgeleads-reset"
_RESET_TOKEN_ISSUER = "bridgeleads"
_RESET_TOKEN_ALGORITHM = "HS256"
_RESET_TOKEN_EXPIRE_SECONDS = 30 * 60  # short-lived: ~30 minutes


def _mint_reset_token(user_id: str) -> str:
    """Mint a short-lived, single-use-able password-reset JWT (A3).

    Fresh jti so TokenBlacklist.consume_once can burn it exactly once on
    redemption. Signed with the app SECRET_KEY/HS256, scoped to the reset
    audience so it is useless on any other endpoint.
    """
    import jwt

    now = int(time.time())
    payload = {
        "sub": user_id,
        "jti": str(uuid.uuid4()),
        "iss": _RESET_TOKEN_ISSUER,
        "aud": _RESET_TOKEN_AUDIENCE,
        "purpose": _RESET_TOKEN_PURPOSE,
        "iat": now,
        "exp": now + _RESET_TOKEN_EXPIRE_SECONDS,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=_RESET_TOKEN_ALGORITHM)


def _decode_reset_token(token: str) -> dict:
    """Decode + verify a password-reset JWT (A3). Raises jwt.InvalidTokenError.

    Pins audience="bridgeleads-reset" and issuer="bridgeleads" (so a session
    token cannot be used here) and checks purpose=="reset". Caller catches
    jwt.InvalidTokenError ONLY and maps it to a generic 400 — any non-JWT
    error must surface, not be masked as "invalid link".
    """
    import jwt

    payload = jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=[_RESET_TOKEN_ALGORITHM],
        audience=_RESET_TOKEN_AUDIENCE,
        issuer=_RESET_TOKEN_ISSUER,
        options={"verify_exp": True},
    )
    if payload.get("purpose") != _RESET_TOKEN_PURPOSE:
        # Right audience but wrong purpose — treat as an invalid reset token.
        raise jwt.InvalidTokenError("not a reset token")
    return payload


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
        # A4: constant-time parity with the success path. The new-account
        # branch below runs bcrypt via hash_password(); burn an equivalent
        # bcrypt cost here so response latency cannot distinguish a
        # registered email (otherwise fast — returns before any hashing)
        # from a new one (slow), closing the account-enumeration timing
        # oracle that the generic error message alone does not.
        hash_password(body.password)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Registration failed. Please try again.",
        )

    trial_end = datetime.now(UTC) + timedelta(days=7)

    # Sprint 7.3: resolve the referral code (if any) to a referrer user.
    # Unknown codes are silently dropped — we don't leak whether a
    # code exists, and a bad code must not block signup.
    referred_by_id: str | None = None
    if body.ref:
        referrer_res = await db.execute(
            select(User).where(
                User.referral_code == body.ref,
                User.is_active,
            )
        )
        referrer = referrer_res.scalar_one_or_none()
        if referrer is not None:
            referred_by_id = referrer.id

    # Generate a unique 8-char referral code for the new user. The
    # alphabet excludes ambiguous characters (0/O, 1/I/L) so the code
    # is unambiguous when shared verbally. Retry on collision — the
    # DB unique constraint is the final authority but we pre-check to
    # avoid most round-trips.
    import secrets
    _ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

    async def _generate_referral_code() -> str:
        for _ in range(8):
            candidate = "".join(secrets.choice(_ALPHABET) for _ in range(8))
            existing_code = await db.execute(
                select(User).where(User.referral_code == candidate)
            )
            if existing_code.scalar_one_or_none() is None:
                return candidate
        # Extremely unlikely after 8 tries with a 30^8 keyspace
        raise RuntimeError("Failed to generate unique referral code")

    referral_code = await _generate_referral_code()

    user = User(
        id=str(uuid.uuid4()),
        email=body.email,
        password_hash=hash_password(body.password),
        plan="pro",
        records_limit=settings.PLAN_LIMITS["pro"],  # 500 records during trial
        trial_ends_at=trial_end,
        referral_code=referral_code,
        referred_by_user_id=referred_by_id,
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
            raise HTTPException(status_code=401, detail="Refresh token already used")
    except _redis_exceptions.RedisError:
        raise revocation_unavailable_503()

    new_access = create_secure_token(user.id)
    new_refresh = create_refresh_token(user.id)
    return TokenResponse(access_token=new_access, refresh_token=new_refresh)


@router.get("/me", response_model=UserResponse)
async def me(current_user: CurrentUser) -> UserResponse:
    return UserResponse.model_validate(current_user)


@router.put("/notification-preferences", response_model=UserResponse)
async def update_notification_preferences(
    body: NotificationPrefsUpdate,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Persist the user's email-notification toggles (settings → Notifications).

    Partial update: only the allowlisted keys the client sent are merged into
    users.notification_prefs (unknown keys are already rejected by the schema's
    extra='forbid'). The WHERE id == current_user.id filter is the tenant guard
    — RLS on `users` is permissive under the app role, so the query filter is the
    real own-row constraint (belt-and-suspenders per the project rules).
    """
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one()
    # Reassign a NEW dict so SQLAlchemy marks the JSON column dirty; in-place
    # mutation of the existing dict would need flag_modified().
    prefs = dict(user.notification_prefs or {})
    prefs.update(body.model_dump(exclude_none=True))
    user.notification_prefs = prefs
    await db.commit()
    await db.refresh(user)
    audit_log(request, "notification_prefs_updated", current_user.id)
    return UserResponse.model_validate(user)


@router.get("/onboarding")
async def onboarding_status(
    current_user: CurrentUser,
    # get_rls_db (not get_db): this route reads the tenant-scoped
    # scraper_configs and jobs tables. Under the non-BYPASSRLS cutover role,
    # a session with no app.current_user_id would return ZERO rows and report
    # the user as having no scrapers/jobs. Setting the GUC keeps it correct.
    db: AsyncSession = Depends(get_rls_db),
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
        "trial_days_remaining": (
            max(0, (current_user.trial_ends_at - datetime.now(UTC)).days)
            if current_user.trial_ends_at else None
        ),
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    current_user: CurrentUser,
) -> None:
    # Extract raw token from Authorization header
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.removeprefix("Bearer ").strip()

    # Logout MUST actually revoke the token. If Redis is unavailable
    # we cannot complete revocation, so surface 503 — silently
    # returning success here would tell the client the token is dead
    # while leaving it usable, which defeats the entire purpose of
    # the logout flow. Other decode errors (already-expired token,
    # malformed token, etc.) are still benign and swallowed below.
    import redis.exceptions as _redis_exceptions

    from src.api.middleware.auth_hardening import TokenBlacklist, revocation_unavailable_503
    try:
        payload = decode_secure_token(token)
        jti = payload.get("jti", "")
        exp = payload.get("exp", 0)
        ttl = max(0, exp - int(time.time()))
        if jti and ttl > 0:
            await TokenBlacklist.add(jti, ttl)
    except _redis_exceptions.RedisError:
        raise revocation_unavailable_503()
    except Exception:
        pass  # Token already invalid — that's fine

    audit_log(request, "logout", current_user.id)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    # logout-all writes the user-revoke timestamp that every JWT decoder
    # checks. If Redis is unavailable, the revocation cannot take effect
    # — surface 503 so the caller knows to retry rather than report a
    # successful logout that did not actually log anyone out.
    import redis.exceptions as _redis_exceptions

    from src.api.middleware.auth_hardening import TokenBlacklist, revocation_unavailable_503
    try:
        await TokenBlacklist.revoke_all_for_user(current_user.id)
    except _redis_exceptions.RedisError:
        raise revocation_unavailable_503()
    # Also revoke the API key. The API-key auth path has no issued-at to
    # compare against the revoke timestamp, so clearing the hash is the only
    # way logout-all ("kill all my credentials") can invalidate a leaked key.
    # The user re-issues via POST /api-key. RedisError above already aborted,
    # so reaching here means the JWT revoke landed.
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one_or_none()
    if user is not None and user.api_key_hash is not None:
        user.api_key_hash = None
        await db.commit()
    audit_log(request, "logout_all", current_user.id)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChange,
    request: Request,
    current_user: CurrentUser,
    # get_rls_db (not get_db): the password-reuse check reads the tenant-scoped
    # password_history table. Without the GUC, under the cutover role that
    # SELECT returns ZERO rows and the "last 5 passwords" reuse block silently
    # passes — a security regression. Setting the RLS context keeps it enforced.
    db: AsyncSession = Depends(get_rls_db),
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

    # A2: a password change MUST evict every existing session and refresh
    # token. Without this, an attacker who already holds a token (or the
    # victim's refresh token) rides straight through the password change —
    # the single most common "I think I've been hacked" reaction would not
    # actually lock them out, and refresh-token rotation (A1) alone lets a
    # held token self-renew. revoke_all_for_user stamps users.revoked_at so
    # every token issued before now is rejected.
    #
    # Revoke BEFORE committing the new password so the two outcomes are
    # fail-safe (Codex review): if revocation fails we 503 with the password
    # UNCHANGED (nothing happened — safe); if revocation succeeds but the
    # password commit later fails, the account is merely logged out and the
    # old password still works (safe). The dangerous ordering is the reverse
    # — password changed but sessions left alive — so we never do that.
    # Revocation is a security boundary: on Redis failure, 503 so the client
    # retries rather than believing the change secured the account.
    import redis.exceptions as _redis_exceptions

    from src.api.middleware.auth_hardening import TokenBlacklist, revocation_unavailable_503
    try:
        await TokenBlacklist.revoke_all_for_user(current_user.id)
    except _redis_exceptions.RedisError:
        raise revocation_unavailable_503()

    # Save current password to history, set the new hash, and ALSO revoke the
    # API key. revoke_all_for_user only invalidates JWTs; the API-key auth path
    # has no issued-at to compare against revoked_at, so a stolen/attacker-
    # created key would survive a password change otherwise — defeating the
    # "secure my account" intent. Clearing api_key_hash matches /logout-all;
    # the user re-issues via POST /api-key. (Codex convergence review.)
    db.add(PasswordHistory(user_id=current_user.id, password_hash=user.password_hash))
    user.password_hash = hash_password(body.new_password)
    user.api_key_hash = None
    await db.commit()
    audit_log(request, "password_changed", current_user.id)


@router.post("/forgot-password", status_code=status.HTTP_200_OK)
async def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """A3: request a password-reset link.

    ENUMERATION-SAFE: ALWAYS returns 200 with the same generic message whether
    or not the email has an account. We never reveal existence — not via the
    body, not via the status code, not via email send/failure, AND not via
    response TIMING: the slow Resend network call is queued as a BACKGROUND
    task that runs AFTER the response is sent, so the existing-email and
    missing-email paths return with the same latency (Codex final review). If a
    matching active user exists we mint a short-lived reset token and email a
    link; otherwise we do nothing observable.
    """
    await rate_limit(request, zone="auth")

    generic = {"message": "If that email exists, a reset link has been sent."}

    result = await db.execute(select(User).where(User.email == body.email, User.is_active))
    user = result.scalar_one_or_none()

    if user is not None:
        token = _mint_reset_token(user.id)
        reset_link = f"{settings.FRONTEND_URL}/reset-password?token={token}"
        # Queue the email AFTER the response so the Resend call's latency can't
        # distinguish an existing account from a missing one. send_password_
        # reset_email soft-fails internally, so a delivery failure never
        # surfaces or disturbs the enumeration-safe 200.
        from src.workers.delivery import send_password_reset_email
        background_tasks.add_task(send_password_reset_email, body.email, reset_link)

    # audit_log records the attempt without leaking existence to the client.
    audit_log(request, "password_reset_requested", user.id if user else None)
    return generic


@router.post("/reset-password", status_code=status.HTTP_200_OK)
async def reset_password(
    body: ResetPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """A3: complete a password reset with a token from /forgot-password.

    Verifies the reset token (distinct audience — a session token cannot be
    used here), single-uses it atomically, rotates the password, writes
    history, and revokes ALL existing sessions BEFORE committing the new
    password (fail-safe ordering).
    """
    await rate_limit(request, zone="auth")

    import jwt

    # Decode + verify. Catch ONLY the JWT error → generic 400. Any other
    # exception (e.g. a SECRET_KEY misconfiguration) must surface as 500,
    # not be masked as "invalid link" (mirrors the /refresh A5 reasoning).
    try:
        payload = _decode_reset_token(body.token)
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset link",
        )

    user_id: str = payload.get("sub", "")
    jti: str = payload.get("jti", "")
    issued_at: int = payload.get("iat", 0)
    exp: int = payload.get("exp", 0)
    ttl = max(0, exp - int(time.time()))
    if not user_id or not jti or ttl <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset link",
        )

    import redis.exceptions as _redis_exceptions

    from src.api.middleware.auth_hardening import TokenBlacklist, revocation_unavailable_503
    from src.db.models import PasswordHistory

    # Look up the subject and enforce the password-reuse policy BEFORE we burn
    # the single-use token or revoke sessions. Doing the validation first means
    # a policy rejection does NOT consume the link or log the user out — they
    # can retry the same link with a compliant password. The valid signed token
    # already proves the caller holds a link we emailed only to a real active
    # user, so this lookup is not an enumeration vector.
    result = await db.execute(select(User).where(User.id == user_id, User.is_active))
    user = result.scalar_one_or_none()
    if user is None:
        # Generic — never reveal whether the subject still exists/is active.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset link",
        )

    # RLS context: this route authenticates via the reset token (no
    # get_rls_db dependency), so set app.current_user_id manually now that the
    # subject is resolved. The password-reuse check below reads the tenant-
    # scoped password_history table — under the non-BYPASSRLS cutover role a
    # session with no GUC returns ZERO rows, silently skipping the reuse block
    # (security regression). session.info lets the after_begin listener
    # re-apply the GUC across the commit at the end. (Codex Phase-2a review.)
    db.sync_session.info["rls_user_id"] = str(user.id)
    await db.execute(
        text("SELECT set_config('app.current_user_id', :uid, true)"),
        {"uid": str(user.id)},
    )

    # Reuse policy (mirrors change_password): a reset link must NOT be a way
    # around the repository's password-reuse rules (Codex final review). Reject
    # the current password and any of the last 5.
    if verify_password(body.new_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password cannot be the same as your current password.",
        )
    history_result = await db.execute(
        select(PasswordHistory)
        .where(PasswordHistory.user_id == user.id)
        .order_by(PasswordHistory.created_at.desc())
        .limit(5)
    )
    for entry in history_result.scalars().all():
        if verify_password(body.new_password, entry.password_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="New password cannot be one of your last 5 passwords.",
            )

    # Token validity + single-use, both fail-closed on Redis error:
    #   1. (Codex convergence) reject any reset token issued at/before the
    #      user's last revoke timestamp. A completed reset calls
    #      revoke_all_for_user (stamping users.revoked_at = now), so ANY OTHER
    #      reset link outstanding at that moment is invalidated — without this,
    #      a second 30-min link could change the password again after a
    #      legitimate reset (or after a logout-all). consume_once only burns the
    #      ONE redeemed jti; this closes the others.
    #   2. atomically claim THIS jti — False => already redeemed or racing a
    #      concurrent redemption.
    try:
        if await TokenBlacklist.is_revoked_by_user_logout_all(user_id, issued_at):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired reset link",
            )
        if not await TokenBlacklist.consume_once(jti, ttl):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Reset link already used",
            )
    except _redis_exceptions.RedisError:
        raise revocation_unavailable_503()

    # A3 + A2 fail-safe ordering (mirrors change_password): revoke ALL sessions
    # BEFORE committing the new password. A reset is the canonical "I've been
    # compromised" action — it MUST evict every live token. If revoke fails we
    # 503 with the password UNCHANGED (safe); if revoke succeeds but the commit
    # later fails, the account is merely logged out and the old password still
    # works (safe). The dangerous reverse never happens.
    try:
        await TokenBlacklist.revoke_all_for_user(user.id)
    except _redis_exceptions.RedisError:
        raise revocation_unavailable_503()

    # Also revoke the API key — a password reset is the canonical compromise-
    # recovery action and MUST evict every credential, not just JWTs (the
    # API-key path never checks revoked_at). Matches /logout-all + change-
    # password; re-issue via POST /api-key. (Codex convergence review.)
    db.add(PasswordHistory(user_id=user.id, password_hash=user.password_hash))
    user.password_hash = hash_password(body.new_password)
    user.api_key_hash = None
    await db.commit()
    audit_log(request, "password_reset", user.id)

    return {"message": "Your password has been reset. Please log in with your new password."}


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
