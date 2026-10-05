"""Login sessions ("Your devices"): one user_sessions row per JWT session family.

Every login path starts its session through start_session(), so the row exists
before any token is handed out. /auth/refresh calls check_session_on_refresh(),
which makes the DB row authoritative for a family's life:
  - revoked_at set   -> refused (the Redis family marker is the fast path for
                        access tokens but can be evicted; this one cannot)
  - older than SESSION_MAX_AGE -> refused: an absolute lifetime, so rotating
                        refresh tokens can no longer keep a session alive forever
  - no row           -> adopted: families minted before this table existed (and
                        legacy tokens with no family at all) get a row on their
                        next refresh, so within an hour every live session is
                        listed and can be signed out.

The user agent is recorded at login, which the browser calls directly (the
login page, MFA, registration and verify-email all POST from the browser), so it
is the real device. Refresh runs on the frontend's server, so an ADOPTED session
records none and shows as an unknown device rather than as "node". It is
display-only text: never trusted, sanitized, capped. No IP is stored or shown.
"""

from __future__ import annotations

import secrets
import unicodedata
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request
from sqlalchemy import or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import create_token_pair
from src.db.models import UserSession

SESSION_MAX_AGE = timedelta(days=30)
# A session is listed while it can still refresh: refresh tokens live 7 days.
SESSION_IDLE_LISTING = timedelta(days=7)
# An access token outlives a family revoke only via Redis; it lasts 1 hour.
ACCESS_TOKEN_LIFETIME = timedelta(hours=1)
_UA_MAX = 256


def client_user_agent(request: Request) -> str | None:
    raw = request.headers.get("user-agent") or ""
    cleaned = "".join(ch for ch in raw if unicodedata.category(ch)[0] != "C").strip()
    return cleaned[:_UA_MAX] or None


async def bind_tenant(db: AsyncSession, user_id: str) -> None:
    """Bind the RLS tenant GUC on a session that started without one (login and
    refresh run before any user is authenticated)."""
    db.sync_session.info["rls_user_id"] = str(user_id)
    await db.execute(
        text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": str(user_id)}
    )


async def start_session(
    db: AsyncSession, request: Request, user_id: str, *, amr: list[str]
) -> tuple[str, str]:
    """Record a new session family, commit it, then mint its token pair.

    Committed BEFORE tokens exist: if the row cannot be written the login fails
    instead of handing out a session that could never be listed or signed out.
    """
    fam = secrets.token_hex(16)
    await bind_tenant(db, user_id)
    db.add(UserSession(id=fam, user_id=str(user_id), user_agent=client_user_agent(request)))
    await db.commit()
    return create_token_pair(user_id, amr=amr, fam=fam)


async def check_session_on_refresh(db: AsyncSession, user_id: str, fam: str) -> None:
    """Enforce the DB-side revoke + absolute lifetime for `fam`, adopting a family
    that has no row yet, and stamp last_seen_at. Raises 401 to end the session."""
    from src.api.middleware.auth_hardening import TokenBlacklist

    await bind_tenant(db, user_id)
    row = (
        await db.execute(
            select(UserSession).where(UserSession.id == fam, UserSession.user_id == str(user_id))
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if row is None:
        # Two racing refreshes of one legacy family both get here; one row wins.
        await db.execute(
            pg_insert(UserSession).values(id=fam, user_id=str(user_id))
            .on_conflict_do_nothing(index_elements=[UserSession.id])
        )
        await db.commit()
        return
    if row.revoked_at is not None:
        await TokenBlacklist.revoke_family(fam)
        raise HTTPException(status_code=401, detail="Refresh token revoked")
    if now - row.created_at >= SESSION_MAX_AGE:
        row.revoked_at = now
        await db.commit()
        await TokenBlacklist.revoke_family(fam)
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
    row.last_seen_at = now
    await db.commit()


async def list_sessions(
    db: AsyncSession, user_id: str, user_revoked_at: datetime | None
) -> list[UserSession]:
    """Sessions that can still be used. Besides per-session revokes, every
    "sign out everywhere" path (logout-all, password change/reset, MFA changes,
    break-glass) stamps users.revoked_at, which kills every token issued before
    it. A killed session can never refresh again, so it is exactly the rows whose
    last_seen_at is not after that stamp."""
    now = datetime.now(UTC)
    stmt = (
        select(UserSession)
        .where(
            UserSession.user_id == str(user_id),
            UserSession.revoked_at.is_(None),
            UserSession.created_at > now - SESSION_MAX_AGE,
            UserSession.last_seen_at > now - SESSION_IDLE_LISTING,
        )
        .order_by(UserSession.last_seen_at.desc())
    )
    if user_revoked_at is not None:
        stmt = stmt.where(UserSession.last_seen_at > user_revoked_at)
    return list((await db.execute(stmt)).scalars())


async def mark_sessions_revoked(db: AsyncSession, user_id: str, fams: list[str]) -> None:
    """Record sign-outs durably. ALWAYS before the Redis family markers: the DB
    row is what refresh trusts, so a crash or Redis failure after this commit
    can never leave a signed-out session able to refresh."""
    if not fams:
        return
    await bind_tenant(db, user_id)
    await db.execute(
        update(UserSession)
        .where(UserSession.user_id == str(user_id), UserSession.id.in_(fams),
               UserSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    await db.commit()


async def revoke_sessions(
    db: AsyncSession, user_id: str, *, only: str | None = None, keep: str | None = None
) -> int:
    """Sign out the user's sessions: just `only`, or every one except `keep`.

    DB first (durable, ends refresh), then the Redis family markers (ends the
    access tokens now; a RedisError propagates for the caller to answer 503).
    Sessions revoked within the access-token lifetime are re-marked in Redis
    too, so retrying after a 503 still ends their access tokens. Returns how
    many sessions matched."""
    from src.api.middleware.auth_hardening import TokenBlacklist

    recent = datetime.now(UTC) - ACCESS_TOKEN_LIFETIME
    stmt = select(UserSession.id, UserSession.revoked_at).where(
        UserSession.user_id == str(user_id),
        or_(UserSession.revoked_at.is_(None), UserSession.revoked_at > recent),
    )
    if only is not None:
        stmt = stmt.where(UserSession.id == only)
    if keep is not None:
        stmt = stmt.where(UserSession.id != keep)
    rows = (await db.execute(stmt)).all()
    await mark_sessions_revoked(db, user_id, [r.id for r in rows if r.revoked_at is None])
    for r in rows:
        await TokenBlacklist.revoke_family(r.id)
    return len(rows)
