"""Logout revokes the session, and a replayed refresh token burns its family.

Audit 2026-09-25, A-1 and A-5. Before: /auth/logout blacklisted only the
presented ACCESS token, so the 7-day refresh token kept minting new ones, and
a refresh token replayed after rotation got a 401 while the thief's (or the
victim's) rotated descendants stayed valid.

Now every login mints a session family (`fam`) carried through every rotation.
Logout revokes the family; a replay outside the 30s grace window revokes the
family; nothing touches the user's OTHER sessions.
"""
from __future__ import annotations

import time
import uuid

import jwt
import pytest
from httpx import AsyncClient

from src.api.routes.auth_helpers import login as login_helpers

_PW = "SecurePass1!"


async def _new_session(client: AsyncClient, email: str | None = None) -> tuple[str, dict]:
    email = email or f"fam_{uuid.uuid4().hex[:8]}@test.bridgeleads.io"
    reg = await client.post("/auth/register", json={
        "first_name": "Fam", "last_name": "Test", "email": email, "password": _PW,
    })
    assert reg.status_code == 201, reg.text
    return email, reg.json()


async def _login(client: AsyncClient, email: str) -> dict:
    r = await client.post("/auth/login", json={"email": email, "password": _PW})
    assert r.status_code == 200, r.text
    return r.json()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_logout_with_the_refresh_token_ends_the_session(client: AsyncClient):
    _, s = await _new_session(client)
    out = await client.post("/auth/logout", json={"refresh_token": s["refresh_token"]})
    assert out.status_code == 204
    again = await client.post("/auth/refresh", json={"refresh_token": s["refresh_token"]})
    assert again.status_code == 401, "logout left the refresh token able to mint new tokens"
    assert (await client.get("/auth/me", headers=_bearer(s["access_token"]))).status_code == 401


async def test_logout_with_only_the_access_token_still_kills_its_refresh_token(client: AsyncClient):
    _, s = await _new_session(client)
    out = await client.post("/auth/logout", headers=_bearer(s["access_token"]))
    assert out.status_code == 204
    again = await client.post("/auth/refresh", json={"refresh_token": s["refresh_token"]})
    assert again.status_code == 401


def _age_past_the_grace_window(redis_client, refresh_token: str) -> None:
    """What 60 real seconds do to Redis: the consumed marker is a minute old and
    the 30s grace-window cache of the rotated pair has expired."""
    jti = jwt.decode(refresh_token, options={"verify_signature": False})["jti"]
    redis_client.set(f"bl:jti:{jti}", f"c:{int(time.time()) - 60}", ex=3600)
    redis_client.delete(f"bl:refresh_replay:{jti}")


async def test_a_replay_after_the_grace_window_burns_the_whole_family(
    client: AsyncClient, redis_client, monkeypatch
):
    monkeypatch.setattr(login_helpers, "_ROTATION_PUBLISH_WAIT_SECONDS", 0.05)
    email, s = await _new_session(client)
    other = await _login(client, email)  # a second, independent session

    rotated = await client.post("/auth/refresh", json={"refresh_token": s["refresh_token"]})
    assert rotated.status_code == 200
    pair2 = rotated.json()

    _age_past_the_grace_window(redis_client, s["refresh_token"])
    replay = await client.post("/auth/refresh", json={"refresh_token": s["refresh_token"]})
    assert replay.status_code == 401

    # The rotated descendants of the replayed token are dead too...
    assert (await client.post("/auth/refresh", json={"refresh_token": pair2["refresh_token"]})).status_code == 401
    assert (await client.get("/auth/me", headers=_bearer(pair2["access_token"]))).status_code == 401
    # ...and the user's OTHER session is untouched.
    assert (await client.get("/auth/me", headers=_bearer(other["access_token"]))).status_code == 200
    assert (await client.post("/auth/refresh", json={"refresh_token": other["refresh_token"]})).status_code == 200


async def test_a_recent_consumption_without_a_published_pair_burns_nothing(
    client: AsyncClient, redis_client, monkeypatch
):
    """The winner of a rotation crashed after consuming the jti and before
    publishing its pair. The retry is refused, but that is not proof of reuse,
    so the session family survives (Codex design review)."""
    monkeypatch.setattr(login_helpers, "_ROTATION_PUBLISH_WAIT_SECONDS", 0.05)
    _, s = await _new_session(client)
    jti = jwt.decode(s["refresh_token"], options={"verify_signature": False})["jti"]
    redis_client.set(f"bl:jti:{jti}", f"c:{int(time.time())}", ex=3600)  # consumed just now
    refused = await client.post("/auth/refresh", json={"refresh_token": s["refresh_token"]})
    assert refused.status_code == 401
    assert (await client.get("/auth/me", headers=_bearer(s["access_token"]))).status_code == 200


async def test_a_race_inside_the_grace_window_still_gets_the_same_pair(client: AsyncClient):
    _, s = await _new_session(client)
    first = await client.post("/auth/refresh", json={"refresh_token": s["refresh_token"]})
    second = await client.post("/auth/refresh", json={"refresh_token": s["refresh_token"]})
    assert first.status_code == second.status_code == 200
    assert first.json()["refresh_token"] == second.json()["refresh_token"]
    # and the session is alive afterwards
    assert (await client.get("/auth/me", headers=_bearer(first.json()["access_token"]))).status_code == 200


@pytest.mark.parametrize("body", [None, {"refresh_token": "not-a-token"}])
async def test_logout_with_nothing_valid_is_refused(client: AsyncClient, body):
    r = await client.post("/auth/logout", json=body) if body else await client.post("/auth/logout")
    assert r.status_code == 401
