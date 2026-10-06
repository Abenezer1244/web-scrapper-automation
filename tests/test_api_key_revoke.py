"""DELETE /auth/api-key: revoke the API key on its own. Real DB + Redis."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import AuditEvent, User


async def _session(client: AsyncClient, user: User) -> dict[str, str]:
    r = await client.post("/auth/login", json={"email": user.email, "password": "TestPass123!"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_revoke_kills_the_key_and_keeps_the_session(
    client: AsyncClient, db: AsyncSession, business_user: User
) -> None:
    auth = await _session(client, business_user)
    raw = (await client.post("/auth/api-key", json={"current_password": "TestPass123!"},
                             headers=auth)).json()["api_key"]
    key = {"Authorization": f"Bearer {raw}"}
    assert (await client.get("/auth/me", headers=key)).status_code == 200

    # The key cannot revoke itself (session only).
    assert (await client.delete("/auth/api-key", headers=key)).status_code == 403
    assert (await client.delete("/auth/api-key", headers=auth)).status_code == 204
    assert (await client.get("/auth/me", headers=key)).status_code == 401, "revoked key still works"
    assert (await client.get("/auth/me", headers=auth)).status_code == 200, "session was signed out"
    assert (await client.delete("/auth/api-key", headers=auth)).status_code == 404
    events = set((await db.execute(
        select(AuditEvent.event).where(AuditEvent.user_id == business_user.id))).scalars())
    assert "api_key_revoked" in events
    feed = (await client.get("/auth/security-events", headers=auth)).json()
    assert "api_key_revoked" in {e["event"] for e in feed}


@pytest.mark.asyncio
async def test_revoke_only_touches_the_callers_key(
    client: AsyncClient, db: AsyncSession, business_user: User, starter_user: User
) -> None:
    owner = await _session(client, business_user)
    raw = (await client.post("/auth/api-key", json={"current_password": "TestPass123!"},
                             headers=owner)).json()["api_key"]
    other = await _session(client, starter_user)
    assert (await client.delete("/auth/api-key", headers=other)).status_code == 404
    assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {raw}"})).status_code == 200


@pytest.mark.asyncio
async def test_a_signed_out_device_cannot_revoke_the_key(
    client: AsyncClient, db: AsyncSession, business_user: User
) -> None:
    old = await _session(client, business_user)
    keeper = await _session(client, business_user)
    raw = (await client.post("/auth/api-key", json={"current_password": "TestPass123!"},
                             headers=keeper)).json()["api_key"]
    assert (await client.post("/auth/sessions/revoke-others", headers=keeper)).status_code == 204
    assert (await client.delete("/auth/api-key", headers=old)).status_code == 401
    assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {raw}"})).status_code == 200
    assert (await client.delete("/auth/api-key", headers=keeper)).status_code == 204
