"""POST /auth/account/delete and /auth/account/restore (account deletion P2a).

Real DB, real Redis, real endpoints and real login sessions (with `fam`, so the
user_sessions check runs). The lifecycle rows are written by the migration-112
functions; these tests only read them.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware import security as _security
from src.config import settings
from src.db.models import AuditEvent, ScraperConfig, User, UserSession
from src.db.session import sync_engine
from src.utils.crypto import encrypt_field

_PW = "TestPass123!"


@pytest.fixture
def deletion_on(monkeypatch):
    monkeypatch.setattr(settings, "ACCOUNT_DELETION_ENABLED", True)


async def _session(client: AsyncClient, user: User) -> dict[str, str]:
    r = await client.post("/auth/login", json={"email": user.email, "password": _PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _delete(client, auth, user, *, password=_PW, email=None, mfa_code=None):
    body = {"current_password": password, "confirm_email": email or user.email}
    if mfa_code is not None:
        body["mfa_code"] = mfa_code
    return await client.post("/auth/account/delete", json=body, headers=auth)


async def _restore(client, auth, *, password=_PW, mfa_code=None):
    body = {"current_password": password}
    if mfa_code is not None:
        body["mfa_code"] = mfa_code
    return await client.post("/auth/account/restore", json=body, headers=auth)


async def _config(db: AsyncSession, user: User, *, active: bool, reason: str | None) -> str:
    cid = str(uuid.uuid4())
    await db.execute(
        text("""
            INSERT INTO scraper_configs (id, user_id, name, county, state, record_type,
                fields, enrichment, schedule, deliver, skip_trace_enabled, active,
                paused_reason)
            VALUES (:id, :u, 'cfg', 'pierce', 'WA', 'probate', '[]'::json, '[]'::json,
                '{}'::json, '{}'::json, false, :active, :reason)
        """),
        {"id": cid, "u": user.id, "active": active, "reason": reason},
    )
    await db.commit()
    return cid


async def _state(db: AsyncSession, user: User) -> str | None:
    return (await db.execute(
        select(User.deletion_state).where(User.id == user.id)
        .execution_options(populate_existing=True)
    )).scalar_one()


async def _configs(db: AsyncSession, user: User) -> dict[str, tuple[bool, str | None]]:
    rows = (await db.execute(
        select(ScraperConfig.id, ScraperConfig.active, ScraperConfig.paused_reason)
        .where(ScraperConfig.user_id == user.id)
    )).all()
    return {r.id: (r.active, r.paused_reason) for r in rows}


async def _events(db: AsyncSession, user: User, event: str) -> int:
    # audit_log persists in fire-and-forget tasks: wait for the real writes.
    await asyncio.gather(*list(_security._audit_tasks))
    return len((await db.execute(
        select(AuditEvent.id).where(AuditEvent.user_id == user.id, AuditEvent.event == event)
    )).all())


@pytest.mark.asyncio
async def test_delete_is_off_until_enabled(client: AsyncClient, starter_user: User) -> None:
    auth = await _session(client, starter_user)
    assert (await _delete(client, auth, starter_user)).status_code == 404
    # Restore is never gated by the flag: "nothing pending" comes from the function.
    r = await _restore(client, auth)
    assert r.status_code == 404 and "not scheduled" in r.json()["detail"]


@pytest.mark.asyncio
async def test_delete_schedules_and_shuts_everything_down_now(
    client: AsyncClient, db: AsyncSession, business_user: User, deletion_on
) -> None:
    running = await _config(db, business_user, active=True, reason=None)
    entitlement = await _config(db, business_user, active=False, reason="entitlement")
    user_paused = await _config(db, business_user, active=False, reason=None)
    auth = await _session(client, business_user)
    login = await client.post("/auth/login", json={"email": business_user.email, "password": _PW})
    other_device = {"Authorization": f"Bearer {login.json()['access_token']}"}
    refresh = login.json()["refresh_token"]
    key = (await client.post("/auth/api-key", json={"current_password": _PW},
                             headers=auth)).json()["api_key"]
    key_auth = {"Authorization": f"Bearer {key}"}

    before = datetime.now(UTC)
    r = await _delete(client, auth, business_user, email=business_user.email.upper())
    assert r.status_code == 200, r.text
    purge_after = datetime.fromisoformat(r.json()["purge_after"])
    # now() + interval '30 days' counts calendar days in the session time zone, so a DST
    # change inside the window moves it by an hour (prod runs UTC); either is far inside
    # CCPA's 45 days.
    assert timedelta(days=29, hours=22) < purge_after - before < timedelta(days=30, hours=2)

    assert await _state(db, business_user) == "pending"
    assert await _configs(db, business_user) == {
        running: (False, "account_deletion"),
        entitlement: (False, "account_deletion"),   # reconciliation can't revive it
        user_paused: (False, None),                  # the user's own pause is kept
    }
    # Every credential is dead: both sessions, refresh rows, and the API key.
    for headers in (auth, other_device, key_auth):
        assert (await client.get("/auth/me", headers=headers)).status_code == 401
    live = (await db.execute(select(UserSession.id).where(
        UserSession.user_id == business_user.id, UserSession.revoked_at.is_(None)))).all()
    assert live == []
    assert (await db.execute(select(User.revoked_at, User.api_key_hash).where(
        User.id == business_user.id).execution_options(populate_existing=True))).one() != (
        None, None)
    assert (await db.execute(select(User.api_key_hash).where(
        User.id == business_user.id))).scalar_one() is None
    r = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 401, "a refresh token outlived the deletion request"
    assert await _events(db, business_user, "account_deletion_requested") == 1


@pytest.mark.asyncio
async def test_a_wrong_confirmation_changes_nothing(
    client: AsyncClient, db: AsyncSession, starter_user: User, deletion_on
) -> None:
    running = await _config(db, starter_user, active=True, reason=None)
    auth = await _session(client, starter_user)
    r = await _delete(client, auth, starter_user, email="someone-else@example.com")
    assert r.status_code == 400 and "email" in r.json()["detail"]
    assert (await _delete(client, auth, starter_user, password="Wrong-pass-1")).status_code == 400
    assert await _state(db, starter_user) is None
    assert await _configs(db, starter_user) == {running: (True, None)}
    assert (await client.get("/auth/me", headers=auth)).status_code == 200
    assert (await db.execute(text("SELECT count(*) FROM account_deletions WHERE user_id = :u"),
                             {"u": starter_user.id})).scalar() == 0


@pytest.mark.asyncio
async def test_a_repeat_request_changes_nothing(
    client: AsyncClient, db: AsyncSession, starter_user: User, deletion_on
) -> None:
    first = await _delete(client, await _session(client, starter_user), starter_user)
    assert first.status_code == 200
    new_session = await _session(client, starter_user)
    # A pending account is gated (P2b): the repeat never reaches the function, whose
    # own idempotency is covered in test_account_deletion_lifecycle.py.
    again = await _delete(client, new_session, starter_user)
    assert again.status_code == 403
    assert await _state(db, starter_user) == "pending"
    assert await _events(db, starter_user, "account_deletion_requested") == 1
    # Nothing was re-run: the session opened after the first request still works.
    assert (await client.get("/auth/me", headers=new_session)).status_code == 200


@pytest.mark.asyncio
async def test_restore_cancels_it_and_leaves_schedules_paused(
    client: AsyncClient, db: AsyncSession, starter_user: User, deletion_on
) -> None:
    running = await _config(db, starter_user, active=True, reason=None)
    assert (await _delete(client, await _session(client, starter_user), starter_user)
            ).status_code == 200
    auth = await _session(client, starter_user)   # signed out everywhere: sign in again
    assert (await _restore(client, auth, password="Wrong-pass-1")).status_code == 400
    assert await _state(db, starter_user) == "pending"
    assert (await _restore(client, auth)).status_code == 204
    assert await _state(db, starter_user) is None
    assert await _configs(db, starter_user) == {running: (False, "account_deletion")}
    assert await _events(db, starter_user, "account_deletion_restored") == 1
    # Restoring twice is not a second transition.
    assert (await _restore(client, auth)).status_code == 404
    assert await _events(db, starter_user, "account_deletion_restored") == 1


@pytest.mark.asyncio
async def test_restore_is_refused_once_the_purge_has_started(
    client: AsyncClient, db: AsyncSession, starter_user: User, deletion_on
) -> None:
    assert (await _delete(client, await _session(client, starter_user), starter_user)
            ).status_code == 200
    # What the P3 claim function will do, done here as the purge role (lock order:
    # users row, then the deletion row).
    with sync_engine.begin() as conn:
        conn.execute(text("SET LOCAL ROLE bridgeleads_purge"))
        conn.execute(text("UPDATE users SET deletion_state = 'purging' WHERE id = :u"),
                     {"u": starter_user.id})
        conn.execute(text("UPDATE account_deletions SET status = 'purging' "
                          "WHERE user_id = :u AND status = 'pending'"), {"u": starter_user.id})
    r = await _restore(client, await _session(client, starter_user))
    assert r.status_code == 409
    assert await _state(db, starter_user) == "purging"


@pytest.mark.asyncio
async def test_two_factor_accounts_must_give_a_code(
    client: AsyncClient, db: AsyncSession, starter_user: User, deletion_on
) -> None:
    secret = pyotp.random_base32()
    auth = await _session(client, starter_user)  # session predates MFA, still valid
    await db.execute(update(User).where(User.id == starter_user.id).values(
        mfa_enabled=True, mfa_secret_encrypted=encrypt_field(secret)))
    await db.commit()
    assert (await _delete(client, auth, starter_user)).status_code == 400
    assert (await _delete(client, auth, starter_user, mfa_code="000000")).status_code == 400
    assert await _state(db, starter_user) is None
    ok = await _delete(client, auth, starter_user, mfa_code=pyotp.TOTP(secret).now())
    assert ok.status_code == 200, ok.text
    assert await _state(db, starter_user) == "pending"


@pytest.mark.asyncio
async def test_api_keys_cannot_delete_or_restore(
    client: AsyncClient, business_user: User, deletion_on
) -> None:
    auth = await _session(client, business_user)
    key = (await client.post("/auth/api-key", json={"current_password": _PW},
                             headers=auth)).json()["api_key"]
    key_auth = {"Authorization": f"Bearer {key}"}
    assert (await _delete(client, key_auth, business_user)).status_code == 403
    assert (await _restore(client, key_auth)).status_code == 403


@pytest.mark.asyncio
async def test_switching_deletion_off_never_strands_a_pending_account(
    client: AsyncClient, db: AsyncSession, starter_user: User, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "ACCOUNT_DELETION_ENABLED", True)
    assert (await _delete(client, await _session(client, starter_user), starter_user)
            ).status_code == 200
    monkeypatch.setattr(settings, "ACCOUNT_DELETION_ENABLED", False)
    assert (await _restore(client, await _session(client, starter_user))).status_code == 204
    assert await _state(db, starter_user) is None


@pytest.mark.asyncio
async def test_restore_needs_the_second_factor_too(
    client: AsyncClient, db: AsyncSession, starter_user: User, deletion_on
) -> None:
    assert (await _delete(client, await _session(client, starter_user), starter_user)
            ).status_code == 200
    auth = await _session(client, starter_user)  # session predates MFA, still valid
    secret = pyotp.random_base32()
    await db.execute(update(User).where(User.id == starter_user.id).values(
        mfa_enabled=True, mfa_secret_encrypted=encrypt_field(secret)))
    await db.commit()
    assert (await _restore(client, auth)).status_code == 400
    assert await _state(db, starter_user) == "pending"
    assert (await _restore(client, auth, mfa_code=pyotp.TOTP(secret).now())).status_code == 204
    assert await _state(db, starter_user) is None


@pytest.mark.asyncio
async def test_no_api_key_can_be_minted_into_a_pending_account(
    client: AsyncClient, db: AsyncSession, business_user: User, deletion_on
) -> None:
    assert (await _delete(client, await _session(client, business_user), business_user)
            ).status_code == 200
    auth = await _session(client, business_user)
    # The gate answers first now (403); the conditional mint stays as the belt for a
    # mint that was already past the gate when the deletion committed.
    r = await client.post("/auth/api-key", json={"current_password": _PW}, headers=auth)
    assert r.status_code == 403, r.text
    assert (await db.execute(select(User.api_key_hash).where(User.id == business_user.id)
                             .execution_options(populate_existing=True))).scalar_one() is None
