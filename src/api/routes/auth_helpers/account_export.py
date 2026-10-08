"""Exporting an account's data (P4c): ask for it, see it, link to it, download it.

  POST /auth/export                      password (+ second factor). Queues a pending
                                         account_exports row (migration 115) for the beat
                                         worker (src/workers/account_export.py), which is
                                         the only builder. One in progress per account
                                         (409), one per 24 h (429; a failed one does not
                                         count). An account pending deletion never gets
                                         here: get_auth_context's gate refuses it (403).
  GET  /auth/export                      the latest export, for Settings.
  GET  /auth/export/{id}/url             a one-minute download link (like the job
                                         export-url).
  GET  /auth/export/{id}/download?token= the ZIP, streamed from R2. Token only: its own
                                         purpose (account_export) and claim (export_id),
                                         so no job token opens it; the jti blacklist and
                                         the logout-all cutoff apply (a deletion request
                                         and an email change both raise the cutoff); and
                                         an account with any deletion state is refused
                                         (owner decision A, 2026-10-07).

Every read filters user_id and runs under the RLS GUC. All four routes are behind
ACCOUNT_EXPORT_ENABLED. Design + review log: tasks/todo-account-deletion.md (P4).
"""

from __future__ import annotations

import uuid

import redis.exceptions as _redis_exceptions
from fastapi import HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware import audit_log
from src.api.schemas import AccountExportResponse
from src.config import settings
from src.db.models import User

from .account_deletion import _second_factor

_URL_TTL = 60
_COLUMNS = "id, status, requested_at, ready_at, expires_at, size_bytes, last_error"
# When the next export may be asked for: 24 h after the latest one that did not fail.
_NEXT_ALLOWED = (
    "GREATEST(now(), COALESCE((SELECT max(requested_at) FROM account_exports "
    "WHERE user_id = :u AND status <> 'failed'), now() - interval '1 day') "
    "+ interval '1 day')"
)
_NOT_FOUND = HTTPException(status.HTTP_404_NOT_FOUND, "Export not found")
_BAD_LINK = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired download link")


def require_enabled() -> None:
    if not settings.ACCOUNT_EXPORT_ENABLED:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


def canonical_id(export_id: str) -> str:
    """A malformed id names no export: the same 404, before any credential check."""
    try:
        return str(uuid.UUID(export_id))
    except ValueError:
        raise _NOT_FOUND from None


def _response(row) -> AccountExportResponse:
    return AccountExportResponse(
        id=str(row.id), status=row.status, requested_at=row.requested_at,
        ready_at=row.ready_at, expires_at=row.expires_at, size_bytes=row.size_bytes,
        last_error=row.last_error if row.status == "failed" else None,
        next_allowed_at=row.next_allowed_at,
    )


async def latest(db: AsyncSession, user_id: str) -> AccountExportResponse | None:
    row = (await db.execute(text(
        f"SELECT {_COLUMNS}, {_NEXT_ALLOWED} AS next_allowed_at FROM account_exports "  # noqa: S608 - fixed literals
        "WHERE user_id = :u ORDER BY requested_at DESC LIMIT 1"), {"u": user_id})).one_or_none()
    return _response(row) if row else None


async def request_export(
    request: Request, db: AsyncSession, user: User, mfa_code: str | None
) -> AccountExportResponse:
    """Caller holds the users row lock (lock_user, which also serialises two clicks)
    and re-proved the password on it. A refusal still commits, so a consumed
    second-factor code stays consumed."""
    user_id = str(user.id)
    await _second_factor(db, user, mfa_code)
    state = (await db.execute(text(
        "SELECT EXISTS (SELECT 1 FROM account_exports WHERE user_id = :u "  # noqa: S608 - fixed literals
        "AND status IN ('pending', 'building')) AS open, "
        f"{_NEXT_ALLOWED} AS next_allowed_at, now() AS now"), {"u": user_id})).one()
    if state.open:
        await db.commit()
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Your data export is already being prepared.")
    if state.next_allowed_at > state.now:
        await db.commit()
        wait = int((state.next_allowed_at - state.now).total_seconds()) + 1
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "You can ask for one data export a day. Try again later.",
            headers={"Retry-After": str(wait)},
        )
    try:
        row = (await db.execute(text(
            f"INSERT INTO account_exports (user_id) VALUES (:u) RETURNING {_COLUMNS}, "  # noqa: S608
            "requested_at + interval '1 day' AS next_allowed_at"), {"u": user_id})).one()
    except IntegrityError:  # the one-open-export index: lost a race we cannot lose
        await db.rollback()  # under the users lock, kept as the belt
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Your data export is already being prepared.") from None
    await db.commit()
    audit_log(request, "account_export_requested", user_id)
    return _response(row)


async def download_url(db: AsyncSession, user_id: str, export_id: str) -> str:
    from src.api.download_tokens import mint_account_export_token

    ready = (await db.execute(text(
        "SELECT 1 FROM account_exports WHERE id = :i AND user_id = :u "
        "AND status = 'ready' AND expires_at > now()"), {"i": export_id, "u": user_id})).first()
    if ready is None:
        raise _NOT_FOUND
    token = mint_account_export_token(user_id, export_id, ttl_seconds=_URL_TTL)
    return f"/auth/export/{export_id}/download?token={token}"


async def resolve_download(db: AsyncSession, token: str, export_id: str) -> tuple[str, int]:
    """(owner id, size) of a ready export the token opens, or refuse. A dedicated
    verifier: the job download's stays exactly as it is."""
    import jwt
    from jwt.exceptions import InvalidTokenError

    from src.api.middleware.auth_hardening import TokenBlacklist, revocation_unavailable_503

    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=["HS256"],
            audience="bridgeleads-download", issuer="bridgeleads",
            options={"require": ["exp", "iat", "aud", "iss", "sub", "jti"]},
        )
    except InvalidTokenError:
        raise _BAD_LINK from None
    user_id, claim = payload.get("sub"), payload.get("export_id")
    if payload.get("purpose") != "account_export" or not isinstance(user_id, str):
        raise _BAD_LINK
    try:
        claim_id = str(uuid.UUID(claim)) if isinstance(claim, str) else None
    except ValueError:
        claim_id = None
    if claim_id != export_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Link not valid for this export")
    try:
        if await TokenBlacklist.is_blacklisted(payload["jti"]):
            raise _BAD_LINK
        if await TokenBlacklist.is_revoked_by_user_logout_all(user_id, payload["iat"]):
            raise _BAD_LINK
    except _redis_exceptions.RedisError:
        raise revocation_unavailable_503() from None

    owner = (await db.execute(text(
        "SELECT 1 FROM users WHERE id = CAST(:u AS uuid) AND is_active "
        "AND deletion_state IS NULL"), {"u": user_id})).first()
    if owner is None:
        raise _BAD_LINK
    # RLS belt for the export read, as the job download does for its rows.
    db.sync_session.info["rls_user_id"] = user_id
    await db.execute(text("SELECT set_config('app.current_user_id', :u, true)"), {"u": user_id})
    size = (await db.execute(text(
        "SELECT size_bytes FROM account_exports WHERE id = :i AND user_id = CAST(:u AS uuid) "
        "AND status = 'ready' AND expires_at > now()"), {"i": export_id, "u": user_id})).scalar()
    if size is None:
        raise _NOT_FOUND
    return user_id, size
