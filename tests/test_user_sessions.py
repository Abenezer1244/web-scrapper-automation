"""Signed-in devices (migration 111): user_sessions rows, listing, sign-out, and
the DB-authoritative revoke + 30-day absolute lifetime enforced at refresh.

Real DB, real Redis, real login/refresh endpoints. No mocks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import create_token_pair, generate_api_key
from src.db.models import User, UserSession

_PW = "TestPass123!"
_CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36"


async def _login(client: AsyncClient, user: User, ua: str = _CHROME) -> dict:
    r = await client.post(
        "/auth/login", json={"email": user.email, "password": _PW},
        headers={"User-Agent": ua},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _auth(tokens: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _refresh(client: AsyncClient, tokens: dict):
    return await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})


async def _rows(db: AsyncSession, user: User) -> list[UserSession]:
    return list((await db.execute(
        select(UserSession).where(UserSession.user_id == user.id)
        .execution_options(populate_existing=True)
    )).scalars())


@pytest.mark.asyncio
async def test_login_records_device_and_lists_it_as_current(
    client: AsyncClient, db: AsyncSession, starter_user: User
) -> None:
    # Header values are Latin-1 on the wire; these are the control bytes that can arrive.
    tokens = await _login(client, starter_user, ua="Safari\x1b[31m on\x7f iPhone\t")
    (row,) = await _rows(db, starter_user)
    assert row.user_agent == "Safari[31m on iPhone", "control chars must be stripped"

    listed = (await client.get("/auth/sessions", headers=_auth(tokens))).json()
    assert len(listed) == 1 and listed[0]["current"] is True
    assert "ip" not in listed[0]


@pytest.mark.asyncio
async def test_revoke_others_ends_them_and_keeps_this_one(
    client: AsyncClient, db: AsyncSession, starter_user: User
) -> None:
    mine = await _login(client, starter_user)
    other = await _login(client, starter_user, ua="Firefox")
    assert len((await client.get("/auth/sessions", headers=_auth(mine))).json()) == 2

    r = await client.post("/auth/sessions/revoke-others", headers=_auth(mine))
    assert r.status_code == 204
    assert (await client.get("/auth/me", headers=_auth(other))).status_code == 401
    assert (await _refresh(client, other)).status_code == 401
    assert (await client.get("/auth/me", headers=_auth(mine))).status_code == 200
    listed = (await client.get("/auth/sessions", headers=_auth(mine))).json()
    assert [s["current"] for s in listed] == [True]


@pytest.mark.asyncio
async def test_revoke_one_session_and_cross_user_is_404(
    client: AsyncClient, starter_user: User, business_user: User
) -> None:
    a = await _login(client, starter_user)
    a2 = await _login(client, starter_user, ua="Edge")
    b = await _login(client, business_user)
    a2_id = next(s["id"] for s in (await client.get("/auth/sessions", headers=_auth(a))).json()
                 if not s["current"])

    # Another account naming A's session id gets nothing and changes nothing.
    assert (await client.delete(f"/auth/sessions/{a2_id}", headers=_auth(b))).status_code == 404
    assert (await client.get("/auth/me", headers=_auth(a2))).status_code == 200

    assert (await client.delete(f"/auth/sessions/{a2_id}", headers=_auth(a))).status_code == 204
    assert (await client.get("/auth/me", headers=_auth(a2))).status_code == 401
    assert (await _refresh(client, a2)).status_code == 401


@pytest.mark.asyncio
async def test_db_revoke_holds_even_if_the_redis_marker_is_lost(
    client: AsyncClient, db: AsyncSession, starter_user: User
) -> None:
    """Redis family markers can be evicted; the DB row must still end the session."""
    tokens = await _login(client, starter_user)
    await db.execute(update(UserSession).where(UserSession.user_id == starter_user.id)
                     .values(revoked_at=datetime.now(UTC)))
    await db.commit()
    assert (await _refresh(client, tokens)).status_code == 401


@pytest.mark.asyncio
async def test_absolute_lifetime_is_30_days(
    client: AsyncClient, db: AsyncSession, starter_user: User
) -> None:
    tokens = await _login(client, starter_user)
    await db.execute(update(UserSession).where(UserSession.user_id == starter_user.id)
                     .values(created_at=datetime.now(UTC) - timedelta(days=30, minutes=1)))
    await db.commit()
    r = await _refresh(client, tokens)
    assert r.status_code == 401 and "sign in again" in r.json()["detail"]
    (row,) = await _rows(db, starter_user)
    assert row.revoked_at is not None

    # A 29-day-old session still refreshes and gets its last_seen stamped.
    fresh = await _login(client, starter_user)
    await db.execute(update(UserSession).where(UserSession.revoked_at.is_(None))
                     .values(created_at=datetime.now(UTC) - timedelta(days=29),
                             last_seen_at=datetime.now(UTC) - timedelta(hours=2)))
    await db.commit()
    assert (await _refresh(client, fresh)).status_code == 200
    live = [r for r in await _rows(db, starter_user) if r.revoked_at is None]
    assert datetime.now(UTC) - live[0].last_seen_at < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_session_from_before_this_table_is_adopted_on_refresh(
    client: AsyncClient, db: AsyncSession, starter_user: User
) -> None:
    access, refresh = create_token_pair(starter_user.id, amr=["pwd"])  # no row, like pre-111
    assert await _rows(db, starter_user) == []
    r = await _refresh(client, {"refresh_token": refresh})
    assert r.status_code == 200
    assert len(await _rows(db, starter_user)) == 1
    listed = (await client.get("/auth/sessions", headers=_auth(r.json()))).json()
    assert len(listed) == 1 and listed[0]["current"] is True


@pytest.mark.asyncio
async def test_logout_and_logout_all_drop_sessions_from_the_list(
    client: AsyncClient, starter_user: User
) -> None:
    a = await _login(client, starter_user)
    b = await _login(client, starter_user)
    c = await _login(client, starter_user)
    await client.post("/auth/logout", headers=_auth(b), json={"refresh_token": b["refresh_token"]})
    assert len((await client.get("/auth/sessions", headers=_auth(a))).json()) == 2

    assert (await client.post("/auth/logout-all", headers=_auth(a))).status_code == 204
    fresh = await _login(client, starter_user)
    listed = (await client.get("/auth/sessions", headers=_auth(fresh))).json()
    assert len(listed) == 1 and listed[0]["current"] is True, "logout-all'd sessions still listed"
    assert (await client.get("/auth/me", headers=_auth(c))).status_code == 401


@pytest.mark.asyncio
async def test_sign_out_needs_a_session_not_an_api_key(
    client: AsyncClient, db: AsyncSession, business_user: User
) -> None:
    await _login(client, business_user)
    raw, key_hash = generate_api_key()
    await db.execute(update(User).where(User.id == business_user.id).values(api_key_hash=key_hash))
    await db.commit()
    key = {"Authorization": f"Bearer {raw}"}
    assert (await client.get("/auth/sessions", headers=key)).status_code == 403
    assert (await client.post("/auth/sessions/revoke-others", headers=key)).status_code == 403
    sid = (await _rows(db, business_user))[0].id
    assert (await client.delete(f"/auth/sessions/{sid}", headers=key)).status_code == 403


@pytest.mark.asyncio
async def test_security_events_are_own_allowlisted_and_carry_no_forensics(
    client: AsyncClient, starter_user: User, business_user: User
) -> None:
    a = await _login(client, starter_user)
    await client.post("/auth/sessions/revoke-others", headers=_auth(a))
    b = await _login(client, business_user)

    mine = (await client.get("/auth/security-events", headers=_auth(a))).json()
    assert {"login_success", "sessions_revoked_others"} <= {e["event"] for e in mine}
    assert all(set(e) == {"event", "created_at"} for e in mine), "ip/path/detail leaked"
    theirs = (await client.get("/auth/security-events", headers=_auth(b))).json()
    assert {e["event"] for e in theirs} == {"login_success"}, "saw another account's events"


@pytest.mark.asyncio
async def test_an_evicted_redis_marker_does_not_revive_a_signed_out_device(
    client: AsyncClient, db: AsyncSession, starter_user: User
) -> None:
    """Revoked in the DB only (as if Redis evicted the family marker): the access
    token must stop working at once, not when it expires."""
    tokens = await _login(client, starter_user)
    assert (await client.get("/auth/me", headers=_auth(tokens))).status_code == 200
    await db.execute(update(UserSession).where(UserSession.user_id == starter_user.id)
                     .values(revoked_at=datetime.now(UTC)))
    await db.commit()
    assert (await client.get("/auth/me", headers=_auth(tokens))).status_code == 401
