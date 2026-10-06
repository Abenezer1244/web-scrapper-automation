"""Account deletion P2b: what an account pending deletion can still do.

Signed in again during the grace period it may see its state (GET /auth/me) and
restore it, nothing else; workers start nothing for it; download links stop. Real DB,
real Redis, real endpoints and login sessions.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.download_tokens import mint_download_token
from src.api.quota import quota_block_reason
from src.config import settings
from src.db.models import User

_PW = "TestPass123!"
_GATED = "This account is scheduled for deletion. Restore it to continue."


@pytest.fixture
def deletion_on(monkeypatch):
    monkeypatch.setattr(settings, "ACCOUNT_DELETION_ENABLED", True)


async def _session(client: AsyncClient, user: User) -> dict[str, str]:
    r = await client.post("/auth/login", json={"email": user.email, "password": _PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _pending(client: AsyncClient, user: User) -> dict[str, str]:
    """Request deletion, then sign in again (the request signed every session out)."""
    r = await client.post(
        "/auth/account/delete",
        json={"current_password": _PW, "confirm_email": user.email},
        headers=await _session(client, user),
    )
    assert r.status_code == 200, r.text
    return await _session(client, user)


@pytest.mark.asyncio
async def test_a_pending_account_can_only_see_its_state_and_restore(
    client: AsyncClient, business_user: User, deletion_on
) -> None:
    auth = await _pending(client, business_user)

    me = await client.get("/auth/me", headers=auth)
    assert me.status_code == 200
    assert me.json()["deletion_state"] == "pending"
    assert datetime.fromisoformat(me.json()["deletion_purge_after"]) > datetime.fromisoformat(
        me.json()["created_at"])

    refused = [
        ("GET", "/scrapers"), ("GET", "/jobs"), ("GET", "/billing/usage"),
        ("GET", "/auth/sessions"), ("GET", "/auth/security-events"),
        ("POST", "/auth/logout-all"),
        ("POST", "/auth/account/delete"),
        ("POST", "/auth/api-key"),
        # The two routes that call get_auth_context directly, with no Request: they
        # must fail closed with the same 403, not 500.
        ("GET", "/scrapers/connectors?include_all=true"),
        ("GET", f"/jobs/{uuid.uuid4()}/download"),
    ]
    for method, path in refused:
        r = await client.request(method, path, headers=auth, json={} if method == "POST" else None)
        assert r.status_code == 403, (method, path, r.status_code, r.text)
        assert r.json()["detail"] == _GATED, (method, path)

    assert (await client.post("/auth/account/restore", json={"current_password": _PW},
                              headers=auth)).status_code == 204
    me = (await client.get("/auth/me", headers=auth)).json()
    assert (me["deletion_state"], me["deletion_purge_after"]) == (None, None)
    assert (await client.get("/scrapers", headers=auth)).status_code == 200


@pytest.mark.asyncio
async def test_signing_in_and_out_still_work_while_pending(
    client: AsyncClient, starter_user: User, deletion_on
) -> None:
    await _pending(client, starter_user)
    login = await client.post("/auth/login", json={"email": starter_user.email, "password": _PW})
    assert login.status_code == 200
    tokens = login.json()
    refreshed = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200
    # A refreshed token is still just a pending account's token.
    fresh = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}
    assert (await client.get("/scrapers", headers=fresh)).status_code == 403
    assert (await client.get("/auth/me", headers=fresh)).status_code == 200


@pytest.mark.asyncio
async def test_no_download_link_works_for_a_pending_account(
    client: AsyncClient, business_user: User, deletion_on
) -> None:
    await _pending(client, business_user)
    job_id = str(uuid.uuid4())
    # Minted more than a second AFTER the request: download tokens are cut off at
    # whole-second precision, so only the deletion_state belt can refuse this one.
    await asyncio.sleep(1.2)
    token = mint_download_token(str(business_user.id), job_id, ttl_seconds=60)
    r = await client.get(f"/jobs/{job_id}/download", params={"token": token})
    assert (r.status_code, r.json()["detail"]) == (401, "User not found"), r.text


@pytest.mark.asyncio
async def test_workers_start_nothing_for_a_pending_account(
    db: AsyncSession, starter_user: User
) -> None:
    assert quota_block_reason(starter_user) is None
    starter_user.deletion_state = "pending"  # in memory only: the gate reads the loaded row
    try:
        assert quota_block_reason(starter_user) == "This account is scheduled for deletion."
    finally:
        db.expunge(starter_user)
