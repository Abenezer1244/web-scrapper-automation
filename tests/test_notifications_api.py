"""Integration tests for the /notifications endpoints (Phase 2b).

DB-backed — CI-arbitrated (prod-only DB). Do NOT run locally.
Uses real conftest fixtures: `client` (httpx AsyncClient), `starter_user`
(committed real User), `starter_token` (JWT bearer for starter_user).
System seeding uses system_sync_session (the worker pathway) so the test
exercises the full read-path: worker-written → API-read.
"""
import json
import uuid

import pytest
from sqlalchemy import text

from src.db.session import system_sync_session

pytestmark = pytest.mark.integration


def _seed_notification(user_id: str, read: bool = False) -> str:
    nid = str(uuid.uuid4())
    with system_sync_session() as db:
        db.execute(
            text("""
                INSERT INTO notifications (id, user_id, type, detail, read_at, created_at)
                VALUES (:i, :u, 'job_completed', CAST(:d AS json),
                        CASE WHEN :r THEN now() ELSE NULL END, now())
            """),
            {"i": nid, "u": user_id, "d": json.dumps({"record_count": 3}), "r": read},
        )
        db.commit()
    return nid


# Real conftest fixtures (verified): `client` is an httpx AsyncClient (no auth);
# `starter_user` is a committed real User; `starter_token` is its JWT bearer.
# Authenticate by passing the bearer header. Tests are async.

def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_list_returns_only_own_with_unread_count(client, starter_user, starter_token):
    _seed_notification(starter_user.id, read=False)
    _seed_notification(starter_user.id, read=True)
    resp = await client.get("/notifications", headers=_auth(starter_token))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 2
    assert body["unread_count"] == 1


@pytest.mark.asyncio
async def test_patch_marks_read(client, starter_user, starter_token):
    nid = _seed_notification(starter_user.id, read=False)
    resp = await client.patch(f"/notifications/{nid}/read", headers=_auth(starter_token))
    assert resp.status_code == 200
    assert resp.json()["read_at"] is not None


@pytest.mark.asyncio
async def test_patch_foreign_id_404(client, starter_token):
    resp = await client.patch(f"/notifications/{uuid.uuid4()}/read", headers=_auth(starter_token))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_malformed_id_404(client, starter_token):
    resp = await client.patch("/notifications/not-a-uuid/read", headers=_auth(starter_token))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_read_all(client, starter_user, starter_token):
    _seed_notification(starter_user.id, read=False)
    _seed_notification(starter_user.id, read=False)
    resp = await client.post("/notifications/read-all", headers=_auth(starter_token))
    assert resp.status_code == 200
    assert resp.json()["updated"] >= 2
    after = await client.get("/notifications", headers=_auth(starter_token))
    assert after.json()["unread_count"] == 0


def test_routes_registered_in_openapi():
    from main import app
    paths = app.openapi()["paths"]
    assert "/notifications" in paths
    assert "/notifications/{notification_id}/read" in paths
    assert "/notifications/read-all" in paths
