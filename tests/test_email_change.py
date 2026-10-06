"""Email change (migration 111): request -> verify new address -> switch.

Real DB, real Redis, real endpoints. The emailed link is minted with the
server's own minter from the real pending row (Resend is not configured in tests,
so nothing is sent); everything after that goes through the public endpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import generate_api_key
from src.api.routes.auth_helpers.email_change import _mint_token
from src.api.routes.auth_helpers.tokens import _mint_reset_token
from src.config import settings
from src.db.models import AuditEvent, PendingEmailChange, User
from src.utils.crypto import encrypt_field
from src.workers.account_emails import _drain_email_change_outbox_impl

_PW = "TestPass123!"


async def _login(client: AsyncClient, email: str, password: str = _PW):
    return await client.post("/auth/login", json={"email": email, "password": password})


async def _session(client: AsyncClient, user: User) -> dict[str, str]:
    r = await _login(client, user.email)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _request(client, auth, new_email, password=_PW, mfa_code=None):
    body = {"new_email": new_email, "current_password": password}
    if mfa_code is not None:
        body["mfa_code"] = mfa_code
    return await client.post("/auth/email/change", json=body, headers=auth)


async def _pending(db: AsyncSession, user: User) -> list[PendingEmailChange]:
    return list((await db.execute(
        select(PendingEmailChange).where(PendingEmailChange.user_id == user.id)
        .order_by(PendingEmailChange.created_at)
        .execution_options(populate_existing=True)
    )).scalars())


def _link_token(row: PendingEmailChange) -> str:
    return _mint_token(row.id, row.user_id, row.expires_at)


async def _confirm(client: AsyncClient, token: str):
    return await client.post("/auth/email/confirm", json={"token": token})


@pytest.mark.asyncio
async def test_full_flow_switches_email_and_signs_out_everywhere(
    client: AsyncClient, db: AsyncSession, starter_user: User
) -> None:
    old_email = starter_user.email
    auth = await _session(client, starter_user)
    raw_key, key_hash = generate_api_key()
    await db.execute(update(User).where(User.id == starter_user.id).values(api_key_hash=key_hash))
    await db.commit()

    r = await _request(client, auth, "  New.Owner@Example.COM ")
    assert r.status_code == 202, r.text
    (row,) = await _pending(db, starter_user)
    assert row.status == "pending" and row.new_email == "new.owner@example.com"

    # Nothing changes until the new address is proven.
    assert (await client.get("/auth/me", headers=auth)).json()["email"] == old_email

    assert (await _confirm(client, _link_token(row))).status_code == 200
    assert (await client.get("/auth/me", headers=auth)).status_code == 401, "sessions survived"
    assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {raw_key}"})
            ).status_code == 401, "API key survived"
    assert (await _login(client, old_email)).status_code == 401
    relogin = await _login(client, "new.owner@example.com")
    assert relogin.status_code == 200, relogin.text
    new_auth = {"Authorization": f"Bearer {relogin.json()['access_token']}"}
    assert (await client.get("/auth/me", headers=new_auth)).json()["email"] == "new.owner@example.com"

    (row,) = await _pending(db, starter_user)
    assert row.status == "confirmed" and row.old_email == old_email
    assert row.notice_state == "pending" and row.stripe_state == "skipped"
    events = set((await db.execute(
        select(AuditEvent.event).where(AuditEvent.user_id == starter_user.id)
    )).scalars())
    assert {"email_change_requested", "email_changed"} <= events

    # Second click: the link is spent.
    assert (await _confirm(client, _link_token(row))).status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("new_email", "password", "status"),
    [("not-an-email", _PW, 422), ("ok@example.com", "WrongPass999!", 400)],
)
async def test_invalid_address_or_wrong_password_creates_nothing(
    client: AsyncClient, db: AsyncSession, starter_user: User, new_email, password, status
) -> None:
    auth = await _session(client, starter_user)
    assert (await _request(client, auth, new_email, password)).status_code == status
    assert await _pending(db, starter_user) == []


@pytest.mark.asyncio
async def test_same_address_is_refused(client: AsyncClient, starter_user: User) -> None:
    auth = await _session(client, starter_user)
    r = await _request(client, auth, starter_user.email.upper())
    assert r.status_code == 400 and "already your email" in r.json()["detail"]


@pytest.mark.asyncio
async def test_taken_address_gets_the_generic_answer_and_no_link(
    client: AsyncClient, db: AsyncSession, starter_user: User, business_user: User
) -> None:
    auth = await _session(client, starter_user)
    r = await _request(client, auth, business_user.email)
    assert r.status_code == 202
    assert r.json() == (await _request(client, auth, "free@example.com")).json()
    rows = await _pending(db, starter_user)
    assert [x.new_email for x in rows if x.status == "pending"] == ["free@example.com"]
    assert all(x.new_email != business_user.email for x in rows), "a link for a taken address"


@pytest.mark.asyncio
async def test_address_taken_after_the_link_was_sent_fails_cleanly(
    client: AsyncClient, db: AsyncSession, starter_user: User, business_user: User
) -> None:
    auth = await _session(client, starter_user)
    await _request(client, auth, "race@example.com")
    (row,) = await _pending(db, starter_user)
    business_user.email = "race@example.com"  # someone else claims it first
    await db.merge(business_user)
    await db.commit()
    r = await _confirm(client, _link_token(row))
    assert r.status_code == 400 and "invalid or has expired" in r.json()["detail"]
    assert (await client.get("/auth/me", headers=auth)).json()["email"] == starter_user.email


@pytest.mark.asyncio
async def test_expired_tampered_and_foreign_tokens_are_refused(
    client: AsyncClient, db: AsyncSession, starter_user: User
) -> None:
    auth = await _session(client, starter_user)
    await _request(client, auth, "later@example.com")
    (row,) = await _pending(db, starter_user)
    good = _link_token(row)
    for bad in (good[:-2] + "xx", _mint_reset_token(starter_user.id), "garbage"):
        assert (await _confirm(client, bad)).status_code == 400
    await db.execute(update(PendingEmailChange).where(PendingEmailChange.id == row.id)
                     .values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    await db.commit()
    assert (await _confirm(client, good)).status_code == 400, "row expiry not enforced"
    assert (await client.get("/auth/me", headers=auth)).status_code == 200


@pytest.mark.asyncio
async def test_a_newer_request_supersedes_the_older_link(
    client: AsyncClient, db: AsyncSession, starter_user: User
) -> None:
    auth = await _session(client, starter_user)
    await _request(client, auth, "first@example.com")
    await _request(client, auth, "second@example.com")
    first, second = await _pending(db, starter_user)
    assert (first.status, second.status) == ("superseded", "pending")
    assert (await _confirm(client, _link_token(first))).status_code == 400
    assert (await _confirm(client, _link_token(second))).status_code == 200


@pytest.mark.asyncio
async def test_two_factor_accounts_must_give_a_code(
    client: AsyncClient, db: AsyncSession, starter_user: User
) -> None:
    secret = pyotp.random_base32()
    auth = await _session(client, starter_user)  # session predates MFA, still valid
    await db.execute(update(User).where(User.id == starter_user.id).values(
        mfa_enabled=True, mfa_secret_encrypted=encrypt_field(secret)))
    await db.commit()
    assert (await _request(client, auth, "mfa@example.com")).status_code == 400
    assert (await _request(client, auth, "mfa@example.com", mfa_code="000000")).status_code == 400
    assert await _pending(db, starter_user) == []
    ok = await _request(client, auth, "mfa@example.com", mfa_code=pyotp.TOTP(secret).now())
    assert ok.status_code == 202, ok.text


@pytest.mark.asyncio
async def test_api_key_cannot_change_the_email(
    client: AsyncClient, db: AsyncSession, business_user: User
) -> None:
    raw, key_hash = generate_api_key()
    await db.execute(update(User).where(User.id == business_user.id).values(api_key_hash=key_hash))
    await db.commit()
    r = await _request(client, {"Authorization": f"Bearer {raw}"}, "key@example.com")
    assert r.status_code == 403
    assert (await client.post("/auth/email/change", json={
        "new_email": "x@example.com", "current_password": _PW})).status_code == 401


@pytest.mark.asyncio
async def test_outbox_waits_without_a_mail_key(
    client: AsyncClient, db: AsyncSession, starter_user: User, monkeypatch
) -> None:
    # The premise is "no key configured" (CI sets a dummy key, which instead
    # exercises the permanent-failure path).
    monkeypatch.setattr(settings, "RESEND_API_KEY", "")
    auth = await _session(client, starter_user)
    await _request(client, auth, "outbox@example.com")
    (row,) = await _pending(db, starter_user)
    await _confirm(client, _link_token(row))
    _drain_email_change_outbox_impl()
    (row,) = await _pending(db, starter_user)
    assert row.notice_state == "pending" and row.outbox_attempts == 0


@pytest.mark.asyncio
async def test_a_throttled_repeat_does_not_strand_the_link_already_sent(
    client: AsyncClient, db: AsyncSession, starter_user: User
) -> None:
    auth = await _session(client, starter_user)
    await _request(client, auth, "again@example.com")
    assert (await _request(client, auth, "again@example.com")).status_code == 202
    (row,) = await _pending(db, starter_user)
    assert row.status == "pending", "the throttled repeat superseded the live link"
    assert (await _confirm(client, _link_token(row))).status_code == 200
