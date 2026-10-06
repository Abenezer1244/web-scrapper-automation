"""Deleting an account: schedule it (30-day grace) or restore it before the purge.

  POST /auth/account/delete   password (+ second factor) + the account's own email typed
                              as confirmation. Opens the deletion through the
                              request_account_deletion() database function (migration
                              112), then, in the same transaction, pauses every
                              schedule and signs out every session and the API key.
  POST /auth/account/restore  the same step-up. Cancels a pending deletion through
                              restore_account_deletion(). Schedules stay paused for
                              the owner to resume.

The lifecycle itself (deletion_state, account_deletions) is written ONLY by those
SECURITY DEFINER functions; this module never writes it. The Stripe cancel/un-cancel
and the "deletion scheduled" email are driven later by the purge beat from the row
(stripe_state / scheduled_email_sent_at), which can record their outcome; a route
cannot. Design + review log: docs/product/account-deletion-and-export.md,
tasks/todo-account-deletion.md.
"""

from __future__ import annotations

from datetime import UTC, datetime

import redis.exceptions as _redis_exceptions
from fastapi import HTTPException, Request, status
from sqlalchemy import or_, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware import MfaFailureGuard, audit_log
from src.api.middleware.auth_hardening import TokenBlacklist, revocation_unavailable_503
from src.db.models import ScraperConfig, User, UserSession
from src.utils.crypto import blind_index

from .tokens import _consume_second_factor

PAUSED_REASON_ACCOUNT_DELETION = "account_deletion"
# SQLSTATEs raised by the migration-112 functions.
_ALREADY_STARTED = "BLD01"
_NOTHING_PENDING = "BLD02"


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)


async def lock_user(db: AsyncSession, user_id: str) -> User:
    """The users row, locked for the rest of the transaction BEFORE the password and
    second factor are checked, so neither can change between the check and the
    lifecycle transition. FOR NO KEY UPDATE: the same lock the migration-112 functions
    take (users row first, everywhere), and it does not wait on child-table inserts."""
    return (
        await db.execute(
            select(User).where(User.id == str(user_id)).with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _second_factor(db: AsyncSession, user: User, mfa_code: str | None) -> None:
    """Require a TOTP or backup code when two-factor is on (email-change pattern).
    The consumed code is committed with the caller's transaction."""
    if not user.mfa_enabled:
        return
    user_id = str(user.id)
    await MfaFailureGuard.ensure_not_locked(user_id)
    if not mfa_code or not await _consume_second_factor(db, user, mfa_code):
        await db.rollback()
        await MfaFailureGuard.record_failure(user_id)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid two-factor code.")
    await MfaFailureGuard.clear(user_id)


async def request_deletion(
    request: Request, db: AsyncSession, user: User, mfa_code: str | None, confirm_email: str
) -> datetime:
    """Caller holds the users row lock (lock_user) and re-proved the password on it.
    Returns the purge date. A repeat request while one is pending changes nothing but
    the consumed second-factor code, and returns its date."""
    user_id = str(user.id)
    await _second_factor(db, user, mfa_code)
    try:
        row = (await db.execute(text("SELECT * FROM request_account_deletion()"))).one()
    except DBAPIError as exc:
        await db.rollback()
        if _sqlstate(exc) == _ALREADY_STARTED:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "This account is already being deleted."
            ) from None
        raise
    # The function holds the users row lock now: compare against the address as it is
    # in this transaction, not as it was when the request arrived.
    email_hmac = (
        await db.execute(select(User.email_hmac).where(User.id == user_id))
    ).scalar_one()
    if blind_index(confirm_email) != email_hmac:
        await db.rollback()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Type your account's email address to confirm."
        )
    if not row.created:
        await db.commit()  # keeps a consumed second-factor code consumed
        return row.purge_after

    now = datetime.now(UTC)
    # Pause every schedule. Entitlement-paused ones too: reconciliation revives only
    # paused_reason='entitlement', and nothing may restart a scrape mid-grace. Ones the
    # user paused stay exactly as they are.
    await db.execute(
        update(ScraperConfig)
        .where(
            ScraperConfig.user_id == user_id,
            or_(ScraperConfig.active, ScraperConfig.paused_reason == "entitlement"),
        )
        .values(active=False, paused_reason=PAUSED_REASON_ACCOUNT_DELETION)
    )
    # Sign out everywhere, in THIS transaction (the users row is locked by it; a
    # separate revoke_all_for_user connection would wait on it). The cutoff ends every
    # access and download token, the session rows end every refresh, and the API key
    # is cleared because its auth path never consults revoked_at.
    await db.execute(
        update(User).where(User.id == user_id).values(revoked_at=now, api_key_hash=None)
    )
    await db.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    try:
        await TokenBlacklist.update_revoke_cache(user_id, now)
    except _redis_exceptions.RedisError:
        await db.rollback()
        raise revocation_unavailable_503() from None
    await db.commit()
    audit_log(request, "account_deletion_requested", user_id)
    return row.purge_after


async def restore_deletion(
    request: Request, db: AsyncSession, user: User, mfa_code: str | None
) -> None:
    """Caller holds the users row lock (lock_user) and re-proved the password on it."""
    user_id = str(user.id)
    await _second_factor(db, user, mfa_code)
    try:
        await db.execute(text("SELECT restore_account_deletion()"))
    except DBAPIError as exc:
        await db.rollback()
        code = _sqlstate(exc)
        if code == _NOTHING_PENDING:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "This account is not scheduled for deletion."
            ) from None
        if code == _ALREADY_STARTED:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Deletion has already started and can no longer be undone.",
            ) from None
        raise
    await db.commit()
    audit_log(request, "account_deletion_restored", user_id)
