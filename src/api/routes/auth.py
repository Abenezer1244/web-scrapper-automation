"""Auth routes: register, login, me, logout, logout-all, api-key.

This module is the FastAPI route layer for /auth. To keep it focused on route
declarations, the large handler bodies live VERBATIM in src/api/routes/auth_helpers/
(grouped by theme: registration, login, mfa, password, session) and each route
here is a thin wrapper that calls its helper with the exact same objects it
received. No route registration, signature, or security logic changed in the
extraction. Small / tightly-coupled handlers remain inline below.

The stateless token primitives (reset + MFA-challenge mint/decode, the login
2nd-factor consume, and their constants) now live in auth_helpers/tokens.py and
are re-exported here so existing `from src.api.routes.auth import X` imports and
the wrappers keep working unchanged.
"""

import time
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import (
    CurrentUser,
    decode_secure_token,
    generate_api_key,
    require_plan,
)
from src.api.deps import get_rls_db
from src.api.middleware import audit_log
from src.api.routes.auth_helpers import login as _login_helpers
from src.api.routes.auth_helpers import mfa as _mfa_helpers
from src.api.routes.auth_helpers import password as _password_helpers
from src.api.routes.auth_helpers import registration as _registration_helpers
from src.api.routes.auth_helpers import session as _session_helpers

# Re-export the stateless token primitives so existing imports of these names
# from src.api.routes.auth keep resolving (rule 4) and so any in-module use is
# unchanged. (noqa: F401 — re-exported for import compatibility.)
from src.api.routes.auth_helpers.tokens import (  # noqa: F401
    _MFA_CHALLENGE_ALGORITHM,
    _MFA_CHALLENGE_AUDIENCE,
    _MFA_CHALLENGE_EXPIRE_SECONDS,
    _MFA_CHALLENGE_ISSUER,
    _MFA_CHALLENGE_PURPOSE,
    _RESET_TOKEN_ALGORITHM,
    _RESET_TOKEN_AUDIENCE,
    _RESET_TOKEN_EXPIRE_SECONDS,
    _RESET_TOKEN_ISSUER,
    _RESET_TOKEN_PURPOSE,
    _consume_second_factor,
    _decode_mfa_challenge_token,
    _decode_reset_token,
    _mint_mfa_challenge_token,
    _mint_reset_token,
)
from src.api.schemas import (
    ApiKeyResponse,
    BreakGlassLoginRequest,
    ForgotPasswordRequest,
    LoginResponse,
    MfaDisableRequest,
    MfaEnableRequest,
    MfaEnableResponse,
    MfaLoginRequest,
    MfaSetupResponse,
    MfaStatusResponse,
    NotificationPrefsUpdate,
    PasswordChange,
    ProfileUpdate,
    RegisterResponse,
    ResetPasswordRequest,
    TokenResponse,
    UserLogin,
    UserRegister,
    UserResponse,
    VerifyEmailRequest,
)
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
            # Single source of truth — a hardcoded copy here drifted to a stale
            # 500 when Pro became 1000 (limits-drift fix, 2026-06-12).
            "records_limit": settings.PLAN_LIMITS["pro"],
        },
        # Lets the frontend render the right signup flow without guessing the
        # backend's posture: when true, /auth/register collects NO password (it
        # is set at /auth/verify-email) and returns a neutral "check your email"
        # response; when false, register takes a password and logs in immediately.
        "email_verification_enabled": settings.EMAIL_VERIFICATION_ENABLED,
    }


@router.post(
    "/register",
    response_model=TokenResponse | RegisterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    body: UserRegister,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse | RegisterResponse:
    """Register a new account.

    Legacy (EMAIL_VERIFICATION_ENABLED off): creates the account and returns
    session tokens (201). Enumeration-safe mode (on): returns a neutral 200 with
    no tokens (same body for new vs existing email) and emails a verification
    link; the handler overrides the status to 200 on that path.
    """
    return await _registration_helpers.register_user(
        body, request, response, background_tasks, db
    )


@router.post("/verify-email", response_model=TokenResponse)
async def verify_email(
    body: VerifyEmailRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Redeem the email-verification link (EMAIL_VERIFICATION_ENABLED flow):
    validate the single-use token, create the account from the staged pending
    registration, and auto-login (mint session tokens)."""
    return await _registration_helpers.verify_user_email(body, request, db)


@router.post("/login", response_model=LoginResponse)
async def login(
    body: UserLogin,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> LoginResponse:
    return await _login_helpers.login_user(body, request, db)


@router.post("/login/mfa", response_model=LoginResponse)
async def login_mfa(
    body: MfaLoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> LoginResponse:
    """Redeem the login MFA challenge (H2-P3): validate the short-lived challenge
    token from /auth/login, verify the 2nd factor, and — only on success — issue
    the real session tokens. The challenge token proves the password step was
    passed; it carries no access privilege on its own."""
    return await _login_helpers.login_mfa_redeem(body, request, db)


@router.post("/login/break-glass", response_model=LoginResponse)
async def login_break_glass(
    body: BreakGlassLoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> LoginResponse:
    """Redeem an operator-issued break-glass code (H2-P5) when the authenticator
    is lost. Reuses the /auth/login challenge token (the password step was already
    proven there) but verifies a BREAK-GLASS code, not TOTP/backup.

    RECOVERY-ONLY + LOUD: on success it tears MFA down to un-enrolled (so the user
    can set up a FRESH authenticator — they can't disable the old one, it's lost),
    burns every remaining break-glass + backup code, revokes all sessions + the
    API key, and mints a DEGRADED session: amr=["pwd","break_glass"] (NO "mfa"),
    so it can never pass admin MFA step-up, and with mfa_enabled now False the
    admin routes route the user to re-enrollment.
    """
    return await _login_helpers.login_break_glass_redeem(body, request, db)


class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    body: RefreshRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Exchange a valid refresh token for a new access + refresh token pair."""
    return await _login_helpers.refresh_tokens(body, request, db)


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


@router.put("/profile", response_model=UserResponse)
async def update_profile(
    body: ProfileUpdate,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Persist the user's editable profile (Settings → Account AND the required-
    name gate): first_name + last_name.

    Both are already sanitized + required (non-empty) by ProfileUpdate. This is
    the endpoint a legacy incomplete-profile user calls to satisfy the gate, so
    it MUST stay reachable while the profile is incomplete (no auth gate on it
    beyond normal login). The WHERE id == current_user.id filter is the tenant
    guard — RLS on `users` is permissive under the app role, so the query filter
    is the real own-row constraint (belt-and-suspenders per the project rules).
    The audit log records the action only, never the name values (they are PII).
    """
    result = await db.execute(select(User).where(User.id == current_user.id))
    user = result.scalar_one()
    user.first_name = body.first_name
    user.last_name = body.last_name
    await db.commit()
    await db.refresh(user)
    audit_log(request, "profile_updated", current_user.id)
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
    return await _session_helpers.onboarding_status_for_user(current_user, db)


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
    return await _password_helpers.change_user_password(body, request, current_user, db)


# ─── MFA (H2): TOTP enrollment ────────────────────────────────────────────────
# Phase 2 = AUTHENTICATED enrollment only. The login MFA challenge (Phase 3) is
# not wired yet, so enabling MFA here does not yet gate /login — it provisions
# the encrypted secret + backup codes and revokes existing sessions.
# mfa_secret_encrypted holds a Fernet token (src/utils/crypto), never the raw
# secret. mfa_backup_codes rows are written under the RLS session with an
# explicit user_id filter. revoke_all_for_user mirrors change-password's
# fail-safe ordering (revoke before commit; 503 on Redis failure).

@router.get("/mfa/status", response_model=MfaStatusResponse)
async def mfa_status(current_user: CurrentUser) -> MfaStatusResponse:
    """Whether the current user has MFA enabled (for settings UI)."""
    return MfaStatusResponse(enabled=bool(current_user.mfa_enabled))


@router.post("/mfa/setup", response_model=MfaSetupResponse)
async def mfa_setup(
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> MfaSetupResponse:
    """Generate a TOTP secret, store it encrypted (NOT yet enabled), and return
    the secret + otpauth URI. Re-calling before enable rotates the pending secret."""
    return await _mfa_helpers.mfa_setup_secret(request, current_user, db)


@router.post("/mfa/enable", response_model=MfaEnableResponse)
async def mfa_enable(
    body: MfaEnableRequest,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> MfaEnableResponse:
    """Verify a TOTP code against the pending secret, enable MFA, return backup
    codes ONCE, and revoke all existing sessions (force a fresh login)."""
    return await _mfa_helpers.mfa_enable_for_user(body, request, current_user, db)


@router.post("/mfa/disable", status_code=status.HTTP_204_NO_CONTENT)
async def mfa_disable(
    body: MfaDisableRequest,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> None:
    """Disable MFA. Requires the password AND a valid second factor (TOTP or an
    unused backup code) — password alone must not remove MFA."""
    return await _mfa_helpers.mfa_disable_for_user(body, request, current_user, db)


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
    return await _password_helpers.forgot_user_password(body, request, background_tasks, db)


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
    return await _password_helpers.reset_user_password(body, request, db)


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
