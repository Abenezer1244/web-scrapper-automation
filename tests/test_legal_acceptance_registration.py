"""Signup records Terms / Privacy acceptance in legal_acceptances (migration 123).

Real Postgres + Redis (no mocks). Compliance audit 2026-11, Phase 3a-ii: the
signup checkbox used to be enforced only in the browser and nothing was stored.

Properties under test:
  * the current versions are recorded, with the database clock, per flow;
  * nothing is recorded (and signup still succeeds) when the fields are absent,
    stale or partial: enforcement is a later, separate step (Phase 3d);
  * in the verified flow acceptance comes ONLY from /auth/verify-email (the person
    who proved the address), never from the unverified register step.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy import delete, select, text

from src.api.routes.auth_helpers.tokens import _mint_verify_token
from src.config import settings
from src.config.constants import LEGAL_DOCUMENT_VERSIONS
from src.db import PendingRegistration, User
from src.utils.crypto import blind_index

_PASSWORD = "SecurePass1!"
_CURRENT = {
    "accepted_terms_version": LEGAL_DOCUMENT_VERSIONS["terms"],
    "accepted_privacy_version": LEGAL_DOCUMENT_VERSIONS["privacy"],
}


def _email() -> str:
    return f"legal_{uuid.uuid4().hex[:10]}@test.bridgeleads.io"


def _legacy(email: str, **extra) -> dict:
    return {"first_name": "Test", "last_name": "User", "email": email,
            "password": _PASSWORD, **extra}


async def _user_id(db, email: str) -> str:
    return (await db.execute(
        select(User.id).where(User.email_hmac == blind_index(email))
    )).scalar_one()


async def _acceptances(db, user_id: str) -> list:
    # The test session is the migration owner, not the RLS-bound app role.
    return (await db.execute(text(
        "SELECT document, version, flow, accepted_at, "
        "abs(extract(epoch FROM (now() - accepted_at))) < 120 AS recent "
        "FROM legal_acceptances WHERE user_id = :u ORDER BY document"
    ), {"u": user_id})).all()


async def test_auth_config_publishes_the_current_versions(client: AsyncClient):
    r = await client.get("/auth/config")
    assert r.status_code == 200
    body = r.json()
    assert body["legal_versions"] == LEGAL_DOCUMENT_VERSIONS
    # The existing keys are unchanged by the new response model.
    assert set(body) == {"password", "trial", "email_verification_enabled", "legal_versions"}


def test_openapi_pins_the_versions_as_constants():
    # The frontend regenerates its types from this schema in CI: a literal here is
    # what turns a backend-only version bump into a frontend typecheck failure.
    # (app.openapi(), not the route: /openapi.json is served only with DEBUG on.)
    from main import app

    props = app.openapi()["components"]["schemas"]["LegalVersions"]["properties"]
    for document, version in LEGAL_DOCUMENT_VERSIONS.items():
        assert props[document].get("const") == version or props[document].get("enum") == [version]


async def test_legacy_signup_records_both_current_documents(client: AsyncClient, db, monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", False)
    email = _email()
    r = await client.post("/auth/register", json=_legacy(email, **_CURRENT))
    assert r.status_code == 201
    rows = await _acceptances(db, await _user_id(db, email))
    assert [(x.document, x.version, x.flow) for x in rows] == [
        ("privacy", LEGAL_DOCUMENT_VERSIONS["privacy"], "register"),
        ("terms", LEGAL_DOCUMENT_VERSIONS["terms"], "register"),
    ]
    assert all(x.recent for x in rows)


async def test_legacy_signup_without_the_fields_still_succeeds_and_records_nothing(
    client: AsyncClient, db, monkeypatch
):
    """Rolling-deploy safety: today's frontend sends no versions."""
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", False)
    email = _email()
    r = await client.post("/auth/register", json=_legacy(email))
    assert r.status_code == 201
    assert await _acceptances(db, await _user_id(db, email)) == []


async def test_stale_or_partial_versions_record_nothing(client: AsyncClient, db, monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", False)
    for extra in (
        {"accepted_terms_version": "2020-01-01",
         "accepted_privacy_version": LEGAL_DOCUMENT_VERSIONS["privacy"]},
        {"accepted_terms_version": LEGAL_DOCUMENT_VERSIONS["terms"]},
    ):
        email = _email()
        r = await client.post("/auth/register", json=_legacy(email, **extra))
        assert r.status_code == 201, extra
        assert await _acceptances(db, await _user_id(db, email)) == [], extra


async def test_a_malformed_version_is_rejected(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", False)
    r = await client.post("/auth/register", json=_legacy(
        _email(), accepted_terms_version="latest", accepted_privacy_version="2026-10-30"))
    assert r.status_code == 422


async def test_verified_flow_records_acceptance_from_the_verifier_only(
    client: AsyncClient, db, monkeypatch
):
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    email = _email()
    try:
        # Versions sent at the unverified register step are ignored.
        r = await client.post("/auth/register", json={"email": email, **_CURRENT})
        assert r.status_code == 200
        pending = (await db.execute(select(PendingRegistration).where(
            PendingRegistration.email_hmac == blind_index(email)))).scalars().one()
        token = _mint_verify_token(pending.id, pending.expires_at)
        r = await client.post("/auth/verify-email", json={
            "token": token, "new_password": _PASSWORD,
            "first_name": "Verifier", "last_name": "Owner", **_CURRENT,
        })
        assert r.status_code == 200
        rows = await _acceptances(db, await _user_id(db, email))
        assert {(x.document, x.flow) for x in rows} == {
            ("terms", "verify_email"), ("privacy", "verify_email")}
    finally:
        await db.execute(delete(PendingRegistration).where(
            PendingRegistration.email_hmac == blind_index(email)))
        await db.commit()


async def test_verified_flow_without_verifier_acceptance_records_nothing(
    client: AsyncClient, db, monkeypatch
):
    """Acceptance sent only at register (by whoever typed the address) is not
    evidence that the address owner accepted anything."""
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", True)
    email = _email()
    try:
        await client.post("/auth/register", json={"email": email, **_CURRENT})
        pending = (await db.execute(select(PendingRegistration).where(
            PendingRegistration.email_hmac == blind_index(email)))).scalars().one()
        token = _mint_verify_token(pending.id, pending.expires_at)
        r = await client.post("/auth/verify-email", json={
            "token": token, "new_password": _PASSWORD,
            "first_name": "Verifier", "last_name": "Owner",
        })
        assert r.status_code == 200
        assert await _acceptances(db, await _user_id(db, email)) == []
    finally:
        await db.execute(delete(PendingRegistration).where(
            PendingRegistration.email_hmac == blind_index(email)))
        await db.commit()
