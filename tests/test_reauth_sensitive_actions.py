"""Credential-changing actions need the password and a real session (audit 2026-09-25, A-2/A-4).

Before: any holder of a one-hour access token could mint a never-expiring API
key, and any holder of an API key (or a stolen session) could enroll a TOTP
second factor the owner does not hold, locking the owner out. Neither asked for
the password, and API keys were accepted for both.
"""
from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select

from src.db.models import User

_PW = "TestPass123!"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _api_key_hash(db, user_id: str) -> str | None:
    return (await db.execute(select(User.api_key_hash).where(User.id == user_id))).scalar_one()


async def test_minting_an_api_key_needs_the_password(
    client: AsyncClient, db, business_user: User, business_token: str
):
    missing = await client.post("/auth/api-key", headers=_bearer(business_token))
    assert missing.status_code == 422

    wrong = await client.post(
        "/auth/api-key", headers=_bearer(business_token), json={"current_password": "not-it"}
    )
    assert wrong.status_code == 400
    assert await _api_key_hash(db, business_user.id) is None, "a wrong password minted a key"

    right = await client.post(
        "/auth/api-key", headers=_bearer(business_token), json={"current_password": _PW}
    )
    assert right.status_code == 201 and right.json()["api_key"].startswith("bl_")


async def test_an_api_key_cannot_mint_its_own_replacement(
    client: AsyncClient, business_user: User, business_token: str
):
    key = (await client.post(
        "/auth/api-key", headers=_bearer(business_token), json={"current_password": _PW}
    )).json()["api_key"]

    # Even WITH the password: an API key is never the session this needs.
    resp = await client.post("/auth/api-key", headers=_bearer(key), json={"current_password": _PW})
    assert resp.status_code == 403


async def test_mfa_enrollment_needs_the_password_and_a_session(
    client: AsyncClient, db, business_user: User, business_token: str
):
    wrong = await client.post(
        "/auth/mfa/setup", headers=_bearer(business_token), json={"current_password": "not-it"}
    )
    assert wrong.status_code == 400
    pending = (await db.execute(
        select(User.mfa_secret_encrypted).where(User.id == business_user.id)
    )).scalar_one()
    assert pending is None, "a wrong password still provisioned a TOTP secret"

    key = (await client.post(
        "/auth/api-key", headers=_bearer(business_token), json={"current_password": _PW}
    )).json()["api_key"]
    via_key = await client.post("/auth/mfa/setup", headers=_bearer(key), json={"current_password": _PW})
    assert via_key.status_code == 403
    enable_via_key = await client.post("/auth/mfa/enable", headers=_bearer(key), json={"code": "123456"})
    assert enable_via_key.status_code == 403

    ok = await client.post(
        "/auth/mfa/setup", headers=_bearer(business_token), json={"current_password": _PW}
    )
    assert ok.status_code == 200 and ok.json()["secret"]


async def test_an_api_key_cannot_change_the_password_or_remove_mfa(
    client: AsyncClient, business_user: User, business_token: str
):
    key = (await client.post(
        "/auth/api-key", headers=_bearer(business_token), json={"current_password": _PW}
    )).json()["api_key"]
    change = await client.post(
        "/auth/change-password", headers=_bearer(key),
        json={"current_password": _PW, "new_password": "BrandNewPass456!"},
    )
    assert change.status_code == 403
    disable = await client.post(
        "/auth/mfa/disable", headers=_bearer(key), json={"password": _PW, "code": "123456"},
    )
    assert disable.status_code == 403
