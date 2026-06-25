"""Tests for the enumeration-safe registration + email-verification flow
(EMAIL_VERIFICATION_ENABLED).

Real Postgres + Redis (no mocks), per the project testing rules. The flag is
toggled per test via monkeypatch (the same pattern test_entitlements_runtime.py
uses for ENTITLEMENT_ENFORCEMENT). Emails are unique per test (uuid) and any
pending_registrations rows created are cleaned up explicitly — conftest's db
fixture only purges the users table.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy import delete, select

from src.api.routes.auth_helpers.tokens import _mint_verify_token
from src.config import settings
from src.db import PendingRegistration, User
from src.utils.crypto import blind_index

_PASSWORD = "SecurePass1!"


def _email() -> str:
    return f"verify_{uuid.uuid4().hex[:10]}@test.bridgeleads.io"


def _reg_body(email: str) -> dict:
    return {"first_name": "Test", "last_name": "User", "email": email, "password": _PASSWORD}


async def _clear_pending(db, email: str) -> None:
    await db.execute(
        delete(PendingRegistration).where(PendingRegistration.email_hmac == blind_index(email))
    )
    await db.commit()


async def test_legacy_flow_returns_tokens_immediately(client: AsyncClient, db, monkeypatch):
    """Flag OFF (default): register behaves exactly as before — 201 + tokens."""
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", False)
    email = _email()
    r = await client.post("/auth/register", json=_reg_body(email))
    assert r.status_code == 201
    assert r.json().get("access_token")  # immediate session
    # user cleaned up by conftest (test domain)


async def test_verified_flow_new_email_is_neutral_with_no_tokens(client: AsyncClient, db, monkeypatch):
    """Flag ON, new email: neutral 200, no tokens, a pending row exists but NO
    real users row yet (the account is created only on verify)."""
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    email = _email()
    r = await client.post("/auth/register", json=_reg_body(email))
    assert r.status_code == 200
    body = r.json()
    assert body.get("verification_required") is True
    assert "access_token" not in body and "refresh_token" not in body

    ehmac = blind_index(email)
    assert (await db.execute(select(User).where(User.email_hmac == ehmac))).scalar_one_or_none() is None
    pending = (
        await db.execute(select(PendingRegistration).where(PendingRegistration.email_hmac == ehmac))
    ).scalar_one_or_none()
    assert pending is not None
    await _clear_pending(db, email)


async def test_verified_flow_existing_and_new_are_indistinguishable(client: AsyncClient, db, monkeypatch):
    """The headline property: a registered email and a brand-new email return
    the IDENTICAL status + body — no enumeration oracle."""
    # Create a real account first (legacy path), then probe under the flag.
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", False)
    existing = _email()
    assert (await client.post("/auth/register", json=_reg_body(existing))).status_code == 201

    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    new = _email()
    r_new = await client.post("/auth/register", json=_reg_body(new))
    r_existing = await client.post("/auth/register", json=_reg_body(existing))

    assert r_new.status_code == r_existing.status_code == 200
    assert r_new.json() == r_existing.json()
    # The existing-email probe must NOT have created a pending row for it.
    assert (
        await db.execute(
            select(PendingRegistration).where(PendingRegistration.email_hmac == blind_index(existing))
        )
    ).scalar_one_or_none() is None
    await _clear_pending(db, new)


async def test_verify_creates_account_and_auto_logs_in(client: AsyncClient, db, monkeypatch):
    """Redeeming the link creates the real account, returns session tokens, drops
    the pending row, and the new account can then log in with its password."""
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    email = _email()
    assert (await client.post("/auth/register", json=_reg_body(email))).status_code == 200

    ehmac = blind_index(email)
    pending = (
        await db.execute(select(PendingRegistration).where(PendingRegistration.email_hmac == ehmac))
    ).scalar_one()
    token = _mint_verify_token(pending.id)

    rv = await client.post("/auth/verify-email", json={"token": token})
    assert rv.status_code == 200
    assert rv.json().get("access_token")

    assert (await db.execute(select(User).where(User.email_hmac == ehmac))).scalar_one_or_none() is not None
    assert (
        await db.execute(select(PendingRegistration).where(PendingRegistration.email_hmac == ehmac))
    ).scalar_one_or_none() is None

    rl = await client.post("/auth/login", json={"email": email, "password": _PASSWORD})
    assert rl.status_code == 200 and rl.json().get("access_token")
    # user cleaned up by conftest


async def test_verify_link_is_single_use(client: AsyncClient, db, monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    email = _email()
    await client.post("/auth/register", json=_reg_body(email))
    pending = (
        await db.execute(
            select(PendingRegistration).where(PendingRegistration.email_hmac == blind_index(email))
        )
    ).scalar_one()
    token = _mint_verify_token(pending.id)

    assert (await client.post("/auth/verify-email", json={"token": token})).status_code == 200
    # Second redemption of the same link must fail (pending consumed / jti burned).
    assert (await client.post("/auth/verify-email", json={"token": token})).status_code == 400


async def test_verify_rejects_invalid_token(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    r = await client.post("/auth/verify-email", json={"token": "not-a-jwt"})
    assert r.status_code == 400


async def test_reregistration_cannot_overwrite_first_pending_password(client: AsyncClient, db, monkeypatch):
    """Account pre-hijacking guard (Codex P1): a second submission for an
    already-pending address must NOT overwrite the first registrant's password.
    Each attempt is its own row, so redeeming the FIRST registrant's link
    activates the FIRST password — the second submitter's password never takes."""
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    email = _email()
    first_pw, second_pw = "FirstPass123!", "SecondPass456!"

    await client.post(
        "/auth/register",
        json={"first_name": "A", "last_name": "One", "email": email, "password": first_pw},
    )
    ehmac = blind_index(email)
    rows = (
        await db.execute(select(PendingRegistration).where(PendingRegistration.email_hmac == ehmac))
    ).scalars().all()
    assert len(rows) == 1
    first_pending_id = rows[0].id

    # A would-be attacker submits the SAME address with a different password.
    await client.post(
        "/auth/register",
        json={"first_name": "B", "last_name": "Two", "email": email, "password": second_pw},
    )
    rows = (
        await db.execute(select(PendingRegistration).where(PendingRegistration.email_hmac == ehmac))
    ).scalars().all()
    assert len(rows) == 2  # SEPARATE rows, not an overwrite

    # Redeem the FIRST registrant's link.
    rv = await client.post("/auth/verify-email", json={"token": _mint_verify_token(first_pending_id)})
    assert rv.status_code == 200

    # The account uses the FIRST password; the second submitter's password is rejected.
    assert (await client.post("/auth/login", json={"email": email, "password": first_pw})).status_code == 200
    assert (await client.post("/auth/login", json={"email": email, "password": second_pw})).status_code == 401
    await _clear_pending(db, email)  # belt-and-suspenders (verify already drops siblings)
