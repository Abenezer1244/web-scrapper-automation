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

import asyncio
import time
import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from jwt.exceptions import InvalidTokenError as JWTError
from pydantic import BaseModel
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import (
    AuthContext,
    CurrentUser,
    decode_secure_token,
    generate_api_key,
    get_auth_context,
    require_plan,
    require_session,
    verify_password,
)
from src.api.deps import get_rls_db
from src.api.middleware import audit_log, rate_limit
from src.api.routes.auth_helpers import account_deletion as _account_deletion
from src.api.routes.auth_helpers import email_change as _email_change
from src.api.routes.auth_helpers import login as _login_helpers
from src.api.routes.auth_helpers import mfa as _mfa_helpers
from src.api.routes.auth_helpers import password as _password_helpers
from src.api.routes.auth_helpers import registration as _registration_helpers
from src.api.routes.auth_helpers import session as _session_helpers
from src.api.routes.auth_helpers import user_sessions as _user_sessions

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
    AccountDeleteRequest,
    AccountDeletionResponse,
    AccountRestoreRequest,
    ApiKeyResponse,
    BreakGlassLoginRequest,
    EmailChangeConfirm,
    EmailChangeRequest,
    ForgotPasswordRequest,
    LoginResponse,
    LogoutRequest,
    MfaDisableRequest,
    MfaEnableRequest,
    MfaEnableResponse,
    MfaLoginRequest,
    MfaSetupResponse,
    MfaStatusResponse,
    NotificationPrefsUpdate,
    PasswordChange,
    ProfileUpdate,
    ReauthRequest,
    RegisterResponse,
    ResetPasswordRequest,
    SecurityEventResponse,
    SessionResponse,
    TokenResponse,
    UserLogin,
    UserRegister,
    UserResponse,
    VerifyEmailRequest,
)
from src.config import settings
from src.config.constants import BUSINESS_FEATURES_PLANS
from src.db import User, get_db  # noqa: F401 (User used in Annotated type)
from src.db.models import AuditEvent, UserAvatar
from src.utils.avatar import ALLOWED_CONTENT_TYPES, MAX_UPLOAD_BYTES, AvatarError, process_avatar

router = APIRouter(prefix="/auth", tags=["auth"])


async def _reauthenticate(request: Request, user: User, password: str) -> None:
    """Re-prove the password before a credential-changing action (A-2/A-4).

    Throttled per ACCOUNT, not per IP: IP keys are not load-bearing in
    production (audit F-01), and a per-account key is what stops a stolen
    session from guessing the password through this endpoint.
    """
    await rate_limit(request, zone="auth", identifier=f"reauth:{user.id}")
    if not verify_password(password, user.password_hash):
        audit_log(request, "reauth_failed", user.id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect.",
        )


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


async def _user_response(db: AsyncSession, user: User) -> UserResponse:
    """UserResponse plus the photo version, which lives in user_avatars.

    Every route that returns UserResponse goes through here: the frontend writes
    these responses straight into its cached profile, so one that omitted the
    photo would blank it until the next refetch. `db` must be the caller's
    RLS-bound session (user_avatars is tenant-scoped); the user_id filter is the
    query-level guard on top of it.
    """
    version = (
        await db.execute(
            select(UserAvatar.version).where(
                UserAvatar.user_id == user.id, UserAvatar.image.is_not(None)
            )
        )
    ).scalar_one_or_none()
    extra: dict = {"avatar_version": version}
    if user.deletion_state is not None:
        # Own row only: the GUC policy on account_deletions plus this user_id filter.
        extra["deletion_purge_after"] = (
            await db.execute(
                text("SELECT purge_after FROM account_deletions WHERE user_id = :u "
                     "AND status IN ('pending', 'purging')"),
                {"u": str(user.id)},
            )
        ).scalar_one_or_none()
    return UserResponse.model_validate(user).model_copy(update=extra)


@router.get("/me", response_model=UserResponse)
async def me(
    current_user: CurrentUser, db: AsyncSession = Depends(get_rls_db)
) -> UserResponse:
    return await _user_response(db, current_user)


@router.put("/notification-preferences", response_model=UserResponse)
async def update_notification_preferences(
    body: NotificationPrefsUpdate,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
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
    return await _user_response(db, user)


@router.put("/profile", response_model=UserResponse)
async def update_profile(
    body: ProfileUpdate,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_rls_db),
) -> UserResponse:
    """Persist the user's editable profile (Settings → Account AND the required-
    name gate): first_name + last_name, and timezone when the caller sends it
    (omitted = unchanged, so the name-only gate never touches it).

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
    if "timezone" in body.model_fields_set:
        user.timezone = body.timezone
    await db.commit()
    await db.refresh(user)
    audit_log(request, "profile_updated", current_user.id)
    return await _user_response(db, user)


# ─── Profile photo ───────────────────────────────────────────────────────────
# The client crops and sends the image as the raw request body (Content-Type
# image/jpeg|png|webp), not multipart: reading the stream ourselves lets us stop
# at MAX_UPLOAD_BYTES instead of spooling an unbounded multipart body first.
# Decoding is CPU-heavy, so it runs off the event loop and at most two at a time
# per process.
_AVATAR_DECODES = asyncio.Semaphore(2)


async def _read_capped_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            "The photo must be 5 MB or smaller.")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_UPLOAD_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                "The photo must be 5 MB or smaller.")
    return bytes(body)


async def _store_avatar(db: AsyncSession, user_id: str, image: bytes | None) -> None:
    """Upsert the user's photo row. A NEW version on every write (also on remove)
    so nothing cached under an old version can be served as the current photo."""
    stmt = pg_insert(UserAvatar).values(
        user_id=user_id, image=image, version=uuid.uuid4().hex
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[UserAvatar.user_id],
        set_={"image": stmt.excluded.image, "version": stmt.excluded.version,
              "updated_at": func.now()},
    )
    await db.execute(stmt)


@router.post("/avatar", response_model=UserResponse)
async def upload_avatar(
    request: Request,
    current_user: Annotated[User, Depends(require_session)],
    db: AsyncSession = Depends(get_rls_db),
) -> UserResponse:
    """Set or replace the profile photo. Signed-in sessions only (not API keys)."""
    await rate_limit(request, zone="avatar", identifier=f"avatar:{current_user.id}")
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                            "Upload a JPEG, PNG or WebP image.")
    raw = await _read_capped_body(request)
    async with _AVATAR_DECODES:
        try:
            webp = await run_in_threadpool(process_avatar, raw)
        except AvatarError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    await _store_avatar(db, current_user.id, webp)
    await db.commit()
    audit_log(request, "avatar_updated", current_user.id)
    return await _user_response(db, current_user)


@router.delete("/avatar", response_model=UserResponse)
async def remove_avatar(
    request: Request,
    current_user: Annotated[User, Depends(require_session)],
    db: AsyncSession = Depends(get_rls_db),
) -> UserResponse:
    """Remove the profile photo; the bytes are erased, not just hidden."""
    await rate_limit(request, zone="avatar", identifier=f"avatar:{current_user.id}")
    await _store_avatar(db, current_user.id, None)
    await db.commit()
    audit_log(request, "avatar_removed", current_user.id)
    return await _user_response(db, current_user)


@router.get("/sessions", response_model=list[SessionResponse])
async def list_my_sessions(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    _session: Annotated[User, Depends(require_session)],
    db: AsyncSession = Depends(get_rls_db),
) -> list[SessionResponse]:
    """The user's signed-in devices, most recently active first."""
    current_fam = (ctx.payload or {}).get("fam")
    rows = await _user_sessions.list_sessions(db, ctx.user.id, ctx.user.revoked_at)
    return [
        SessionResponse.model_validate(r).model_copy(update={"current": r.id == current_fam})
        for r in rows
    ]


async def _revoke_my_sessions(
    request: Request, db: AsyncSession, user: User, *, only: str | None = None,
    keep: str | None = None,
) -> int:
    import redis.exceptions as _redis_exceptions

    from src.api.middleware.auth_hardening import revocation_unavailable_503

    await rate_limit(request, zone="auth", identifier=f"sessions:{user.id}")
    try:
        return await _user_sessions.revoke_sessions(db, user.id, only=only, keep=keep)
    except _redis_exceptions.RedisError:
        raise revocation_unavailable_503()


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_my_session(
    session_id: str,
    request: Request,
    current_user: Annotated[User, Depends(require_session)],
    db: AsyncSession = Depends(get_rls_db),
) -> None:
    """Sign out one device. Only the caller's own sessions can match."""
    if not await _revoke_my_sessions(request, db, current_user, only=session_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found.")
    audit_log(request, "session_revoked", current_user.id)


@router.post("/sessions/revoke-others", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_my_other_sessions(
    request: Request,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    _session: Annotated[User, Depends(require_session)],
    db: AsyncSession = Depends(get_rls_db),
) -> None:
    """Sign out every device except this one."""
    keep = (ctx.payload or {}).get("fam")
    await _revoke_my_sessions(request, db, ctx.user, keep=keep)
    audit_log(request, "sessions_revoked_others", ctx.user.id)


@router.post("/email/change", status_code=status.HTTP_202_ACCEPTED)
async def request_email_change(
    body: EmailChangeRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(require_session)],
    db: AsyncSession = Depends(get_rls_db),
) -> dict:
    """Email a confirmation link to the new address. Same answer whether or not
    that address already has an account."""
    await _reauthenticate(request, current_user, body.current_password)
    user = (await db.execute(select(User).where(User.id == current_user.id))).scalar_one()
    await _email_change.request_email_change(
        request, background_tasks, db, user, str(body.new_email), body.mfa_code
    )
    return {"message": "Check the new address for a confirmation link. It expires in 1 hour."}


@router.post("/email/confirm")
async def confirm_email_change(
    body: EmailChangeConfirm, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    """Redeem the emailed link. The token is the credential, so no session is
    needed (the link may be opened on another device). Signs out every session."""
    await _email_change.confirm_email_change(request, db, body.token)
    return {"message": "Your email address was changed. Sign in with the new address."}


@router.post("/account/delete", response_model=AccountDeletionResponse)
async def delete_account(
    body: AccountDeleteRequest,
    request: Request,
    current_user: Annotated[User, Depends(require_session)],
    db: AsyncSession = Depends(get_rls_db),
) -> AccountDeletionResponse:
    """Schedule this account for deletion in 30 days. Pauses every schedule and signs
    out every device and the API key now; signing in again and restoring undoes it."""
    if not settings.ACCOUNT_DELETION_ENABLED:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    user = await _account_deletion.lock_user(db, current_user.id)
    await _reauthenticate(request, user, body.current_password)
    purge_after = await _account_deletion.request_deletion(
        request, db, user, body.mfa_code, body.confirm_email
    )
    return AccountDeletionResponse(purge_after=purge_after)


@router.post("/account/restore", status_code=status.HTTP_204_NO_CONTENT)
async def restore_account(
    body: AccountRestoreRequest,
    request: Request,
    current_user: Annotated[User, Depends(require_session)],
    db: AsyncSession = Depends(get_rls_db),
) -> None:
    """Cancel a pending deletion. Never gated by ACCOUNT_DELETION_ENABLED: switching
    deletion off must not strand an account that is already pending."""
    user = await _account_deletion.lock_user(db, current_user.id)
    await _reauthenticate(request, user, body.current_password)
    await _account_deletion.restore_deletion(request, db, user, body.mfa_code)


# Security activity a user should be able to recognise (or not) as their own.
# An allowlist, not "everything with my user_id": the audit log also holds
# operational events (job_created, ...) that are not security activity.
SECURITY_EVENTS = (
    "login_success", "password_changed", "password_reset", "mfa_enabled",
    "mfa_disabled", "mfa_breakglass_used", "api_key_created", "api_key_revoked",
    "email_change_requested",
    "email_changed", "session_revoked", "sessions_revoked_others", "logout_all",
    "account_deletion_requested", "account_deletion_restored",
)


@router.get("/security-events", response_model=list[SecurityEventResponse])
async def my_security_events(
    current_user: CurrentUser, db: AsyncSession = Depends(get_rls_db)
) -> list[SecurityEventResponse]:
    """The user's 20 most recent security events (Settings > Security)."""
    rows = await db.execute(
        select(AuditEvent.event, AuditEvent.created_at)
        .where(AuditEvent.user_id == current_user.id, AuditEvent.event.in_(SECURITY_EVENTS))
        .order_by(AuditEvent.created_at.desc())
        .limit(20)
    )
    return [SecurityEventResponse(event=e, created_at=t) for e, t in rows.all()]


@router.get("/avatar", responses={200: {"content": {"image/webp": {}}}})
async def get_avatar(
    current_user: CurrentUser,
    v: str | None = None,
    db: AsyncSession = Depends(get_rls_db),
) -> Response:
    """The signed-in user's photo as WebP. `v` is the avatar_version from /auth/me:
    a request for the CURRENT version is cacheable forever (the version changes
    with the photo); any other version is served uncached."""
    row = (
        await db.execute(
            select(UserAvatar.image, UserAvatar.version).where(
                UserAvatar.user_id == current_user.id
            )
        )
    ).one_or_none()
    if row is None or row.image is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No profile photo.")
    cache = ("private, max-age=31536000, immutable" if v == row.version
             else "private, no-store")
    return Response(
        content=row.image,
        media_type="image/webp",
        # Vary: a browser shared by two accounts must never answer one user's
        # request from the other's cached photo.
        headers={"Cache-Control": cache, "Vary": "Authorization",
                 "X-Content-Type-Options": "nosniff"},
    )


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
    body: LogoutRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> None:
    """End THIS session: the access token (bearer), the refresh token (body), or both.

    Audit 2026-09-25, A-1: logout used to blacklist only the access token, so
    the 7-day refresh token kept minting new ones. Each presented token now has
    its jti blacklisted AND its session family revoked, which ends every token
    that session ever rotated into. The refresh token is accepted on its own so
    logout still works once the 1-hour access token has expired. The user's
    other sessions are untouched (that is /auth/logout-all).
    """
    import redis.exceptions as _redis_exceptions

    from src.api.auth import decode_refresh_token
    from src.api.middleware.auth_hardening import TokenBlacklist, revocation_unavailable_503

    auth_header = request.headers.get("Authorization", "")
    access_token = auth_header.removeprefix("Bearer ").strip()
    presented: list[dict] = []
    if access_token:
        try:
            presented.append(decode_secure_token(access_token))
        except JWTError:
            pass  # expired or not a session token; the refresh token may still be given
    if body and body.refresh_token:
        try:
            refresh_payload = decode_refresh_token(body.refresh_token)
            if refresh_payload.get("purpose") == "refresh":
                presented.append(refresh_payload)
        except JWTError:
            pass
    if not presented:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    # Durable record FIRST: the session row is what refresh trusts, so even if
    # Redis fails below (or later evicts the markers) this session can never
    # refresh again. Both tokens were signed by us and carry their own sub +
    # fam, so no tenant can be named here but the token's own.
    for payload in presented:
        if payload.get("sub") and payload.get("fam"):
            await _user_sessions.mark_sessions_revoked(db, payload["sub"], [payload["fam"]])

    # Logout MUST actually revoke. If Redis is unavailable we cannot, so 503:
    # reporting success would tell the client the session is dead while it
    # is still usable.
    try:
        for payload in presented:
            jti = payload.get("jti", "")
            ttl = max(0, int(payload.get("exp", 0)) - int(time.time()))
            if jti and ttl > 0:
                await TokenBlacklist.add(jti, ttl)
            if payload.get("fam"):
                await TokenBlacklist.revoke_family(payload["fam"])
    except _redis_exceptions.RedisError:
        raise revocation_unavailable_503()

    audit_log(request, "logout", presented[0].get("sub"))


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
    # A session, not an API key (A-4): a key holder has no business changing
    # how the owner signs in, even one who also knows the password.
    current_user: Annotated[User, Depends(require_session)],
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
    body: ReauthRequest,
    request: Request,
    current_user: Annotated[User, Depends(require_session)],
    db: AsyncSession = Depends(get_rls_db),
) -> MfaSetupResponse:
    """Generate a TOTP secret, store it encrypted (NOT yet enabled), and return
    the secret + otpauth URI. Re-calling before enable rotates the pending secret.

    Needs the password and a signed-in session (audit 2026-09-25, A-4): whoever
    enrolls the second factor controls the account, so a stolen session or a
    leaked API key must not be able to enroll one the owner does not hold."""
    await _reauthenticate(request, current_user, body.current_password)
    return await _mfa_helpers.mfa_setup_secret(request, current_user, db)


@router.post("/mfa/enable", response_model=MfaEnableResponse)
async def mfa_enable(
    body: MfaEnableRequest,
    request: Request,
    current_user: Annotated[User, Depends(require_session)],
    db: AsyncSession = Depends(get_rls_db),
) -> MfaEnableResponse:
    """Verify a TOTP code against the pending secret, enable MFA, return backup
    codes ONCE, and revoke all existing sessions (force a fresh login)."""
    return await _mfa_helpers.mfa_enable_for_user(body, request, current_user, db)


@router.post("/mfa/disable", status_code=status.HTTP_204_NO_CONTENT)
async def mfa_disable(
    body: MfaDisableRequest,
    request: Request,
    current_user: Annotated[User, Depends(require_session)],  # never an API key (A-4)
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
    body: ReauthRequest,
    request: Request,
    current_user: Annotated[User, Depends(require_plan(*sorted(BUSINESS_FEATURES_PLANS)))],
    _session: Annotated[User, Depends(require_session)],
    db: AsyncSession = Depends(get_db),
) -> ApiKeyResponse:
    """Generate a new API key. The raw key is shown exactly once.

    A key never expires, so minting one needs the password and a signed-in
    session (audit 2026-09-25, A-2): a stolen one-hour token, or a leaked key,
    must not be able to turn itself into a permanent credential.
    """
    await _reauthenticate(request, current_user, body.current_password)
    raw_key, key_hash = generate_api_key()

    # Conditional, not read-then-assign: an account deletion signs out everything and
    # clears the key under the users row lock. A mint that started before it blocks on
    # that lock and, under READ COMMITTED, re-checks this WHERE against the committed
    # row, so it cannot write a fresh key into an account that is now pending deletion
    # (API-key auth never consults revoked_at).
    minted = await db.execute(
        update(User)
        .where(User.id == current_user.id, User.deletion_state.is_(None))
        .values(api_key_hash=key_hash)
    )
    if minted.rowcount == 0:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This account is scheduled for deletion."
        )

    audit_log(request, "api_key_created", current_user.id)
    return ApiKeyResponse(api_key=raw_key)


@router.delete("/api-key", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    request: Request,
    current_user: Annotated[User, Depends(require_session)],
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke the account's API key on its own (without signing out everywhere).

    Signed-in session only, like creating one: a leaked key must not be able to
    act on itself. Takes effect on the next request, since the key is checked
    against this hash every time. 404 when there is no key to revoke.
    """
    await rate_limit(request, zone="auth", identifier=f"api-key-revoke:{current_user.id}")
    # Conditional + rowcount: two concurrent revokes cannot both report success.
    cleared = await db.execute(
        update(User)
        .where(User.id == current_user.id, User.api_key_hash.is_not(None))
        .values(api_key_hash=None)
    )
    if cleared.rowcount == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "There is no API key to revoke.")
    audit_log(request, "api_key_revoked", current_user.id)
