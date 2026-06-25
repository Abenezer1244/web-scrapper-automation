"""Tests for the enumeration-safe registration + email-verification flow
(EMAIL_VERIFICATION_ENABLED).

Real Postgres + Redis (no mocks), per the project testing rules. The flag is
toggled per test via monkeypatch (the same pattern test_entitlements_runtime.py
uses for ENTITLEMENT_ENFORCEMENT). Emails are unique per test (uuid) and any
pending_registrations rows created are cleaned up explicitly — conftest's db
fixture only purges the users table.

Key property under test: the password is set at /auth/verify-email by whoever
proves email control, NOT at register — so an attacker-initiated signup that the
address owner confirms cannot end up with an attacker-known password.
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


def _legacy_body(email: str) -> dict:
    """Legacy flow requires a password at register."""
    return {"first_name": "Test", "last_name": "User", "email": email, "password": _PASSWORD}


def _verified_body(email: str) -> dict:
    """Verified flow collects NO password at register (set at verify)."""
    return {"first_name": "Test", "last_name": "User", "email": email}


async def _clear_pending(db, email: str) -> None:
    await db.execute(
        delete(PendingRegistration).where(PendingRegistration.email_hmac == blind_index(email))
    )
    await db.commit()


async def _pending_for(db, email: str):
    return (
        await db.execute(
            select(PendingRegistration).where(PendingRegistration.email_hmac == blind_index(email))
        )
    ).scalars().all()


async def test_legacy_flow_returns_tokens_immediately(client: AsyncClient, db, monkeypatch):
    """Flag OFF (default): register behaves exactly as before — 201 + tokens."""
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", False)
    email = _email()
    r = await client.post("/auth/register", json=_legacy_body(email))
    assert r.status_code == 201
    assert r.json().get("access_token")  # immediate session


async def test_verified_flow_new_email_is_neutral_with_no_tokens(client: AsyncClient, db, monkeypatch):
    """Flag ON, new email: neutral 200, no tokens, a pending row exists but NO
    real users row yet (the account is created only on verify)."""
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    email = _email()
    r = await client.post("/auth/register", json=_verified_body(email))
    assert r.status_code == 200
    body = r.json()
    assert body.get("verification_required") is True
    assert "access_token" not in body and "refresh_token" not in body

    ehmac = blind_index(email)
    assert (await db.execute(select(User).where(User.email_hmac == ehmac))).scalar_one_or_none() is None
    assert len(await _pending_for(db, email)) == 1
    await _clear_pending(db, email)


async def test_verified_flow_existing_and_new_are_indistinguishable(client: AsyncClient, db, monkeypatch):
    """The headline property: a registered email and a brand-new email return
    the IDENTICAL status + body — no enumeration oracle."""
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", False)
    existing = _email()
    assert (await client.post("/auth/register", json=_legacy_body(existing))).status_code == 201

    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    new = _email()
    r_new = await client.post("/auth/register", json=_verified_body(new))
    r_existing = await client.post("/auth/register", json=_verified_body(existing))

    assert r_new.status_code == r_existing.status_code == 200
    assert r_new.json() == r_existing.json()
    # The existing-email probe must NOT have created a pending row for it.
    assert len(await _pending_for(db, existing)) == 0
    await _clear_pending(db, new)


async def test_verify_sets_the_verifier_password_and_auto_logs_in(client: AsyncClient, db, monkeypatch):
    """Redeeming the link creates the real account with the password supplied AT
    VERIFY (not at register), returns session tokens, drops the pending row, and
    the account can then log in with that password."""
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    email = _email()
    chosen_pw = "ChosenAtVerify9!"
    assert (await client.post("/auth/register", json=_verified_body(email))).status_code == 200

    ehmac = blind_index(email)
    pending = (await _pending_for(db, email))[0]
    token = _mint_verify_token(pending.id)

    rv = await client.post("/auth/verify-email", json={"token": token, "new_password": chosen_pw})
    assert rv.status_code == 200
    assert rv.json().get("access_token")

    assert (await db.execute(select(User).where(User.email_hmac == ehmac))).scalar_one_or_none() is not None
    assert len(await _pending_for(db, email)) == 0

    rl = await client.post("/auth/login", json={"email": email, "password": chosen_pw})
    assert rl.status_code == 200 and rl.json().get("access_token")


async def test_verify_link_is_single_use(client: AsyncClient, db, monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    email = _email()
    await client.post("/auth/register", json=_verified_body(email))
    pending = (await _pending_for(db, email))[0]
    token = _mint_verify_token(pending.id)

    assert (
        await client.post("/auth/verify-email", json={"token": token, "new_password": _PASSWORD})
    ).status_code == 200
    # Second redemption of the same link must fail (pending consumed).
    assert (
        await client.post("/auth/verify-email", json={"token": token, "new_password": _PASSWORD})
    ).status_code == 400


async def test_verify_rejects_invalid_token(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    r = await client.post("/auth/verify-email", json={"token": "not-a-jwt", "new_password": _PASSWORD})
    assert r.status_code == 400


async def test_reregistration_creates_separate_pending_rows(client: AsyncClient, db, monkeypatch):
    """Pre-hijacking guard: a second submission for an already-pending address
    creates its OWN row (no upsert/overwrite). Combined with set-password-at-verify,
    the address owner who clicks their link sets their own password regardless of
    any other pending row."""
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    email = _email()
    await client.post("/auth/register", json=_verified_body(email))
    await client.post("/auth/register", json=_verified_body(email))
    assert len(await _pending_for(db, email)) == 2  # SEPARATE rows, not an overwrite
    await _clear_pending(db, email)
