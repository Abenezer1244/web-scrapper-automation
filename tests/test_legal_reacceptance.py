"""Terms / Privacy re-acceptance (compliance Phase 3d, src/api/legal_acceptance.py).

Real Postgres + Redis. Users are the conftest fixtures; acceptance rows they write are
removed with the user (legal_acceptances.user_id ON DELETE CASCADE is the account purge's
business, so these tests delete their own rows in `finally`).
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from src.api.auth import generate_api_key
from src.api.legal_acceptance import _EXEMPT
from src.config import settings
from src.config.constants import LEGAL_DOCUMENT_VERSIONS, PREVIOUS_PRIVACY_VERSION

_CURRENT = {"accepted_terms_version": LEGAL_DOCUMENT_VERSIONS["terms"],
            "accepted_privacy_version": LEGAL_DOCUMENT_VERSIONS["privacy"]}
_GATED = "/auth/onboarding"   # an ordinary signed-in route, not exempt


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _rows(db, user_id) -> list[tuple[str, str, str]]:
    return [tuple(r) for r in (await db.execute(text(
        "SELECT document, version, flow FROM legal_acceptances WHERE user_id = :u "
        "ORDER BY document, accepted_at"), {"u": str(user_id)})).all()]


async def _cleanup(db, user_id) -> None:
    await db.execute(text("DELETE FROM legal_acceptances WHERE user_id = :u"),
                     {"u": str(user_id)})
    await db.commit()


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setattr(settings, "LEGAL_REACCEPT_ENFORCED", True)


async def test_status_says_required_until_both_current_versions_are_accepted(
        client, db, business_user, business_token):
    try:
        r = await client.get("/auth/legal", headers=_bearer(business_token))
        assert r.status_code == 200
        assert r.json()["required"] is True
        assert r.json()["terms"] == {"current": LEGAL_DOCUMENT_VERSIONS["terms"],
                                     "accepted": False}
        r = await client.post("/auth/legal/accept", json=_CURRENT,
                              headers=_bearer(business_token))
        assert (r.status_code, r.json()["required"]) == (200, False)
        assert await _rows(db, business_user.id) == [
            ("privacy", LEGAL_DOCUMENT_VERSIONS["privacy"], "reaccept"),
            ("terms", LEGAL_DOCUMENT_VERSIONS["terms"], "reaccept")]
        # Accepting again records nothing new.
        await client.post("/auth/legal/accept", json=_CURRENT, headers=_bearer(business_token))
        assert len(await _rows(db, business_user.id)) == 2
    finally:
        await _cleanup(db, business_user.id)


async def test_the_previous_privacy_version_is_not_current_acceptance(
        client, db, business_user, business_token):
    try:
        await db.execute(text("SELECT set_config('app.current_user_id', :u, true)"),
                         {"u": str(business_user.id)})
        await db.execute(text(
            "INSERT INTO legal_acceptances (user_id, document, version, flow) VALUES "
            "(:u, 'terms', :t, 'register'), (:u, 'privacy', :p, 'register')"),
            {"u": str(business_user.id), "t": LEGAL_DOCUMENT_VERSIONS["terms"],
             "p": PREVIOUS_PRIVACY_VERSION})
        await db.commit()
        r = await client.get("/auth/legal", headers=_bearer(business_token))
        assert r.json()["required"] is True
        assert r.json()["privacy"]["accepted"] is False
    finally:
        await _cleanup(db, business_user.id)


async def test_stale_versions_record_nothing(client, db, business_user, business_token):
    try:
        r = await client.post("/auth/legal/accept",
                              json={**_CURRENT, "accepted_privacy_version": "2001-01-01"},
                              headers=_bearer(business_token))
        assert (r.status_code, r.json()["detail"]) == (409, "legal_version_changed")
        assert await _rows(db, business_user.id) == []
    finally:
        await _cleanup(db, business_user.id)


async def test_an_api_key_cannot_accept_for_its_owner(client, db, business_user):
    raw, key_hash = generate_api_key()
    business_user.api_key_hash = key_hash
    await db.commit()
    try:
        r = await client.post("/auth/legal/accept", json=_CURRENT, headers=_bearer(raw))
        assert r.status_code == 403
        assert await _rows(db, business_user.id) == []
    finally:
        await _cleanup(db, business_user.id)


async def test_the_gate_is_off_by_default(client, business_token):
    assert (await client.get(_GATED, headers=_bearer(business_token))).status_code == 200


async def test_enforced_it_blocks_sessions_and_api_keys_but_never_the_exempt_routes(
        client, db, business_user, business_token, enforced):
    raw, key_hash = generate_api_key()
    business_user.api_key_hash = key_hash
    await db.commit()
    try:
        for headers in (_bearer(business_token), _bearer(raw)):
            r = await client.get(_GATED, headers=headers)
            assert (r.status_code, r.json()["detail"]) == (403, "legal_acceptance_required")
        for path in ("/auth/me", "/auth/legal", "/auth/mfa/status", "/auth/sessions"):
            assert (await client.get(path, headers=_bearer(business_token))).status_code == 200
        await client.post("/auth/legal/accept", json=_CURRENT, headers=_bearer(business_token))
        for headers in (_bearer(business_token), _bearer(raw)):
            assert (await client.get(_GATED, headers=headers)).status_code == 200
    finally:
        await _cleanup(db, business_user.id)


async def test_signup_needs_exactly_the_current_versions_when_required(client, monkeypatch):
    monkeypatch.setattr(settings, "LEGAL_ACCEPTANCE_REQUIRED", True)
    monkeypatch.setattr(settings, "EMAIL_VERIFICATION_ENABLED", False)
    form = {"email": "reaccept-signup@test.bridgeleads.io", "password": "TestPass123!x",
            "first_name": "Ann", "last_name": "Example"}
    for versions in ({}, {**_CURRENT, "accepted_privacy_version": PREVIOUS_PRIVACY_VERSION}):
        r = await client.post("/auth/register", json=form | versions)
        assert (r.status_code, r.json()["detail"]) == (422, "legal_acceptance_required")


async def test_verify_email_refuses_before_touching_the_link(client, monkeypatch):
    monkeypatch.setattr(settings, "LEGAL_ACCEPTANCE_REQUIRED", True)
    r = await client.post("/auth/verify-email", json={
        "token": "x" * 40, "new_password": "TestPass123!x",
        "first_name": "Ann", "last_name": "Example"})
    assert (r.status_code, r.json()["detail"]) == (422, "legal_acceptance_required")


def test_every_exemption_names_a_real_route():
    """A typo would silently gate a route a person must always reach. The app's own
    route table (its OpenAPI paths: included routers are nested, not flat)."""
    from main import app
    routes = {(m.upper(), path) for path, ops in app.openapi()["paths"].items() for m in ops}
    assert sorted(_EXEMPT - routes) == []
