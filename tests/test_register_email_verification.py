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
_VERIFY_FIRST = "Verifier"
_VERIFY_LAST = "Owner"


def _email() -> str:
    return f"verify_{uuid.uuid4().hex[:10]}@test.bridgeleads.io"


def _legacy_body(email: str) -> dict:
    """Legacy flow requires a password AND name at register."""
    return {"first_name": "Test", "last_name": "User", "email": email, "password": _PASSWORD}


def _verified_body(email: str) -> dict:
    """Verified flow collects ONLY email at register (password + name set at verify)."""
    return {"email": email}


def _verify_body(token: str, password: str = _PASSWORD) -> dict:
    """Verify body: password AND name are set HERE by the verifier (not at register)."""
    return {
        "token": token,
        "new_password": password,
        "first_name": _VERIFY_FIRST,
        "last_name": _VERIFY_LAST,
    }


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
    token = _mint_verify_token(pending.id, pending.expires_at)

    rv = await client.post("/auth/verify-email", json=_verify_body(token, chosen_pw))
    assert rv.status_code == 200
    assert rv.json().get("access_token")

    # The created account's NAME comes from the verifier (set at verify), NOT from
    # whatever was submitted at register — closes the attacker-set-display-name gap.
    user = (await db.execute(select(User).where(User.email_hmac == ehmac))).scalar_one_or_none()
    assert user is not None
    assert user.first_name == _VERIFY_FIRST and user.last_name == _VERIFY_LAST
    assert len(await _pending_for(db, email)) == 0

    rl = await client.post("/auth/login", json={"email": email, "password": chosen_pw})
    assert rl.status_code == 200 and rl.json().get("access_token")


async def test_verify_link_is_single_use(client: AsyncClient, db, monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    email = _email()
    await client.post("/auth/register", json=_verified_body(email))
    pending = (await _pending_for(db, email))[0]
    token = _mint_verify_token(pending.id, pending.expires_at)

    assert (
        await client.post("/auth/verify-email", json=_verify_body(token))
    ).status_code == 200
    # Second redemption of the same link must fail (pending consumed).
    assert (
        await client.post("/auth/verify-email", json=_verify_body(token))
    ).status_code == 400


async def test_verify_rejects_invalid_token(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    r = await client.post("/auth/verify-email", json=_verify_body("not-a-jwt"))
    assert r.status_code == 400


async def test_verified_register_does_not_persist_submitted_name(client: AsyncClient, db, monkeypatch):
    """Even if a name is submitted at register, the verified flow must NOT store it
    on the pending row — the name is set by the verifier. Otherwise an attacker
    could seed a victim-verified account's display name (the gap #6 closes)."""
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    email = _email()
    r = await client.post(
        "/auth/register",
        json={"email": email, "first_name": "Attacker", "last_name": "Chosen"},
    )
    assert r.status_code == 200
    pending = (await _pending_for(db, email))[0]
    assert pending.first_name is None and pending.last_name is None
    await _clear_pending(db, email)


async def test_reset_link_uses_url_fragment(client: AsyncClient, db, monkeypatch):
    """#7: the password-reset link carries the token in the URL fragment (#token=),
    not the query string, so it can't leak via server logs / Referer."""
    import src.workers.delivery as delivery

    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", False)
    captured: list[str] = []
    monkeypatch.setattr(delivery, "send_password_reset_email", lambda email, link: captured.append(link))
    email = _email()
    assert (await client.post("/auth/register", json=_legacy_body(email))).status_code == 201

    r = await client.post("/auth/forgot-password", json={"email": email})
    assert r.status_code == 200
    assert captured and "/reset-password#token=" in captured[0]
    assert "/reset-password?token=" not in captured[0]


async def test_legacy_register_requires_name(client: AsyncClient, monkeypatch):
    """Legacy flow (flag off): a missing first/last name is a 422, like a missing
    password — name is only Optional on the schema to support the verified flow."""
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", False)
    r = await client.post(
        "/auth/register",
        json={"email": _email(), "password": _PASSWORD},  # no first/last name
    )
    assert r.status_code == 422


async def test_verify_requires_name(client: AsyncClient, db, monkeypatch):
    """Verified flow: the verify body must carry first/last name (422 if absent)."""
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    email = _email()
    await client.post("/auth/register", json=_verified_body(email))
    pending = (await _pending_for(db, email))[0]
    token = _mint_verify_token(pending.id, pending.expires_at)
    r = await client.post(
        "/auth/verify-email", json={"token": token, "new_password": _PASSWORD}  # no name
    )
    assert r.status_code == 422
    await _clear_pending(db, email)


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


def _patch_recording_sender(monkeypatch) -> list:
    """Replace the real Resend send with an in-memory recorder.

    Not a data mock — it only intercepts the external email side effect (so the
    suite never actually hits Resend), which the testing rules permit for an
    external API. Returns the list that receives (email, verify_link) tuples.
    """
    import src.workers.onboarding_emails as oe

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(oe, "send_verification_email", lambda email, link: sent.append((email, link)))
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_key")
    return sent


async def test_dispatcher_sends_due_row_and_marks_sent(client: AsyncClient, db, monkeypatch):
    """The outbox dispatcher sends a fresh pending row's verification email and
    records 'sent' + verification_email_sent_at on the row."""
    from src.workers.scheduler_helpers.registration import (
        _dispatch_pending_verification_emails_impl,
    )

    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    sent = _patch_recording_sender(monkeypatch)
    email = _email()
    await client.post("/auth/register", json=_verified_body(email))

    _dispatch_pending_verification_emails_impl()

    assert len(sent) == 1 and sent[0][0] == email
    assert "/verify-email#token=" in sent[0][1]  # token in fragment, not query
    rows = await _pending_for(db, email)
    assert len(rows) == 1
    assert rows[0].email_dispatch_state == "sent"
    assert rows[0].verification_email_sent_at is not None
    await _clear_pending(db, email)


async def test_dispatcher_suppresses_rapid_duplicate_address(client: AsyncClient, db, monkeypatch):
    """Email-bomb guard: two pending rows for the SAME address within the window
    yield exactly ONE real send; the duplicate is marked 'suppressed' (no send)
    and does not poison the guard."""
    from src.workers.scheduler_helpers.registration import (
        _dispatch_pending_verification_emails_impl,
    )

    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    sent = _patch_recording_sender(monkeypatch)
    email = _email()
    await client.post("/auth/register", json=_verified_body(email))
    await client.post("/auth/register", json=_verified_body(email))

    _dispatch_pending_verification_emails_impl()

    assert len(sent) == 1  # only ONE email despite two rows
    states = sorted(r.email_dispatch_state for r in await _pending_for(db, email))
    assert states == ["sent", "suppressed"]
    await _clear_pending(db, email)


async def test_dispatcher_retries_transient_failure(client: AsyncClient, db, monkeypatch):
    """A transient send error keeps the row 'pending', bumps email_attempts, and
    pushes next_email_attempt_at into the future (backoff) for the next tick."""
    import requests

    import src.workers.onboarding_emails as oe
    from src.workers.scheduler_helpers.registration import (
        _dispatch_pending_verification_emails_impl,
    )

    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_key")

    def _boom(email, link):
        raise requests.RequestException("transient")

    monkeypatch.setattr(oe, "send_verification_email", _boom)
    email = _email()
    await client.post("/auth/register", json=_verified_body(email))

    _dispatch_pending_verification_emails_impl()

    row = (await _pending_for(db, email))[0]
    assert row.email_dispatch_state == "pending"  # not sent, not failed — will retry
    assert row.email_attempts == 1
    assert row.verification_email_sent_at is None
    await _clear_pending(db, email)


async def test_dispatcher_permanent_failure_marks_failed(client: AsyncClient, db, monkeypatch):
    """A permanent (non-retryable) send error marks the row 'failed' so the beat
    stops retrying it."""
    import src.workers.onboarding_emails as oe
    from src.workers.scheduler_helpers.registration import (
        _dispatch_pending_verification_emails_impl,
    )

    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_key")

    def _permanent(email, link):
        raise ValueError("malformed payload")  # non-retryable per _is_retryable_email_error

    monkeypatch.setattr(oe, "send_verification_email", _permanent)
    email = _email()
    await client.post("/auth/register", json=_verified_body(email))

    _dispatch_pending_verification_emails_impl()

    row = (await _pending_for(db, email))[0]
    assert row.email_dispatch_state == "failed"
    assert row.email_attempts == 1
    await _clear_pending(db, email)


async def test_dispatcher_noop_when_flag_off(client: AsyncClient, db, monkeypatch):
    """With the flag off the dispatcher sends nothing, even if a pending row
    exists from a prior on-period."""
    from src.workers.scheduler_helpers.registration import (
        _dispatch_pending_verification_emails_impl,
    )

    # Create a pending row with the flag ON, then turn it OFF before dispatch.
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    sent = _patch_recording_sender(monkeypatch)
    email = _email()
    await client.post("/auth/register", json=_verified_body(email))

    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", False)
    _dispatch_pending_verification_emails_impl()

    assert sent == []
    rows = await _pending_for(db, email)
    assert rows[0].email_dispatch_state == "pending"  # untouched
    await _clear_pending(db, email)
