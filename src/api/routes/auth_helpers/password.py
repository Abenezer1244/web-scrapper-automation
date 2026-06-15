"""Body logic for the password routes: POST /auth/change-password,
/auth/forgot-password, /auth/reset-password. Extracted VERBATIM from auth.py —
the route decorators + signatures stay in auth.py; these hold the moved bodies.
"""

import time

from fastapi import BackgroundTasks, HTTPException, Request, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import hash_password, verify_password
from src.api.middleware import audit_log, rate_limit
from src.api.schemas import ForgotPasswordRequest, PasswordChange, ResetPasswordRequest
from src.config import settings
from src.db import User
from src.utils.crypto import blind_index

from .tokens import _decode_reset_token, _mint_reset_token


async def change_user_password(
    body: PasswordChange,
    request: Request,
    current_user: User,
    db: AsyncSession,
) -> None:
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


async def forgot_user_password(
    body: ForgotPasswordRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession,
) -> dict:
    await rate_limit(request, zone="auth")

    generic = {"message": "If that email exists, a reset link has been sent."}

    # H3: match by the email blind index (email is encrypted at rest).
    result = await db.execute(
        select(User).where(User.email_hmac == blind_index(body.email), User.is_active)
    )
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


async def reset_user_password(
    body: ResetPasswordRequest,
    request: Request,
    db: AsyncSession,
) -> dict:
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
