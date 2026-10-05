"""Changing the account email: prove the password (and second factor), then prove
the NEW address, then switch.

  POST /auth/email/change   signed-in session + current password (+ MFA code when
                            enabled). Supersedes any earlier request, records a
                            NEW pending row (never rewrites one), emails the new
                            address a one-hour link and tells the current address
                            a change was requested. The answer is the same whether
                            or not the new address already has an account.
  POST /auth/email/confirm  the emailed token is the credential (the user may open
                            the link on another device). Switches the email, signs
                            out every session and clears the API key, and queues
                            the old-address notice + Stripe customer email sync on
                            the row (drained by beat, retried, never lost).

The token is bound to one pending row (sub) and its user (uid); a row is only
confirmable while 'pending' and unexpired, and confirming flips it atomically, so
a link works once and an older link can never confirm a newer request.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import BackgroundTasks, HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware import MfaFailureGuard, audit_log, once_per, rate_limit
from src.config import settings
from src.db.models import PendingEmailChange, User
from src.utils.crypto import blind_index, normalize_email

from .tokens import _consume_second_factor
from .user_sessions import bind_tenant

_TOKEN_AUDIENCE = "bridgeleads-email-change"
_TOKEN_ISSUER = "bridgeleads"
_TOKEN_PURPOSE = "email_change"
_LINK_TTL = timedelta(hours=1)
# One verification email per target address per window, whoever asks.
_SEND_INTERVAL_SECONDS = 120
_GENERIC_CONFIRM_FAILURE = (
    "This link is invalid or has expired. Request a new one from Settings."
)


def _mint_token(pending_id: str, user_id: str, expires_at: datetime) -> str:
    payload = {
        "sub": pending_id,
        "uid": str(user_id),
        "jti": str(uuid.uuid4()),
        "iss": _TOKEN_ISSUER,
        "aud": _TOKEN_AUDIENCE,
        "purpose": _TOKEN_PURPOSE,
        "iat": int(time.time()),
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def _decode_token(token: str) -> dict:
    payload = jwt.decode(
        token, settings.SECRET_KEY, algorithms=["HS256"],
        audience=_TOKEN_AUDIENCE, issuer=_TOKEN_ISSUER, options={"verify_exp": True},
    )
    if payload.get("purpose") != _TOKEN_PURPOSE or not payload.get("uid"):
        raise jwt.InvalidTokenError("not an email-change token")
    return payload


async def request_email_change(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession,
    user: User,
    new_email: str,
    mfa_code: str | None,
) -> None:
    """Caller has already re-proved the password. Raises 400 only for problems the
    user can fix on their own account (missing/invalid code, same address)."""
    await rate_limit(request, zone="auth", identifier=f"email-change:{user.id}")
    new_email = normalize_email(new_email)
    new_hmac = blind_index(new_email)
    if new_hmac == user.email_hmac:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That is already your email address.")

    user_id, current_email = str(user.id), user.email  # rollback/commit expire `user`
    if user.mfa_enabled:
        await MfaFailureGuard.ensure_not_locked(user_id)
        if not mfa_code or not await _consume_second_factor(db, user, mfa_code):
            await db.rollback()
            await MfaFailureGuard.record_failure(user_id)
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid two-factor code.")
        await MfaFailureGuard.clear(user_id)

    # Called UNCONDITIONALLY so a taken address costs the same round-trip as a
    # free one (no timing oracle for "does this address have an account").
    fresh = await once_per(f"emailchg:{new_hmac}", _SEND_INTERVAL_SECONDS)
    taken = (
        await db.execute(select(User.id).where(User.email_hmac == new_hmac))
    ).scalar_one_or_none() is not None

    # Only a request that will actually send a link replaces the previous one: a
    # throttled or taken-address request must not strand a link already sent.
    expires_at = datetime.now(UTC) + _LINK_TTL
    pending_id = None
    if fresh and not taken:
        await bind_tenant(db, user_id)
        await db.execute(
            update(PendingEmailChange)
            .where(PendingEmailChange.user_id == user_id, PendingEmailChange.status == "pending")
            .values(status="superseded")
        )
        pending_id = str(uuid.uuid4())
        db.add(PendingEmailChange(
            id=pending_id, user_id=user_id, new_email=new_email, expires_at=expires_at
        ))
    try:
        # Always commit: it also keeps a consumed second-factor code consumed.
        await db.commit()
    except IntegrityError:
        # The one-pending-per-user index: a concurrent request won the race.
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Another email change is in progress. Try again."
        ) from None

    if pending_id is not None:
        from src.workers.account_emails import (
            send_email_change_requested_notice,
            send_email_change_verification,
        )
        token = _mint_token(pending_id, user_id, expires_at)
        # Fragment, not query: never reaches a server log or a Referer header.
        link = f"{settings.FRONTEND_URL}/confirm-email#token={token}"
        background_tasks.add_task(send_email_change_verification, new_email, link)
        background_tasks.add_task(send_email_change_requested_notice, current_email)
    audit_log(request, "email_change_requested", user_id)


async def confirm_email_change(request: Request, db: AsyncSession, token: str) -> None:
    import redis.exceptions as _redis_exceptions

    from src.api.middleware.auth_hardening import TokenBlacklist, revocation_unavailable_503

    await rate_limit(request, zone="auth")
    try:
        payload = _decode_token(token)
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _GENERIC_CONFIRM_FAILURE) from None
    user_id = payload["uid"]
    await bind_tenant(db, user_id)

    now = datetime.now(UTC)
    # Flip pending -> confirmed atomically: a second click (or a racing one) finds
    # nothing to flip. Expiry is the row's, not only the token's.
    pending = (
        await db.execute(
            update(PendingEmailChange)
            .where(
                PendingEmailChange.id == payload["sub"],
                PendingEmailChange.user_id == user_id,
                PendingEmailChange.status == "pending",
                PendingEmailChange.expires_at > now,
            )
            .values(status="confirmed", confirmed_at=now)
            .returning(PendingEmailChange)
        )
    ).scalar_one_or_none()
    user = (
        await db.execute(select(User).where(User.id == user_id, User.is_active))
    ).scalar_one_or_none()
    if pending is None or user is None:
        await db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _GENERIC_CONFIRM_FAILURE)

    pending.old_email = user.email
    pending.notice_state = "pending"
    pending.stripe_state = "pending" if user.stripe_customer_id else "skipped"
    pending.next_outbox_attempt_at = now
    user.email = pending.new_email  # re-derives email_hmac (UNIQUE = the race guard)
    # Sign out everywhere, in THIS transaction (the users row is now ours; a
    # separate revoke_all_for_user connection would wait on it). Clear the API
    # key too: its auth path never consults revoked_at.
    user.revoked_at = now
    user.api_key_hash = None
    try:
        await db.flush()
    except IntegrityError:
        # The address got an account after the link was sent. Same answer as any
        # other dead link, so the link holder learns nothing about that account.
        await db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _GENERIC_CONFIRM_FAILURE) from None
    try:
        await TokenBlacklist.update_revoke_cache(user_id, now)
    except _redis_exceptions.RedisError:
        await db.rollback()
        raise revocation_unavailable_503() from None
    await db.commit()
    audit_log(request, "email_changed", user_id)
