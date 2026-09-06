"""The /auth/refresh contract the web session depends on.

Regression cover for the "Stripe Checkout Back button logs the user out" defect.

Root cause: the backend issues 1-HOUR access tokens while the web session lives
7 DAYS, and the frontend held a frozen access token with no way to rotate it. Any
401 was then read as "signed out" and destroyed a perfectly valid session — most
visibly on the Billing -> Stripe Checkout -> browser Back round trip, which
refetches several billing queries on return.

The fix rotates the access token through /auth/refresh instead of signing out.
These tests pin the three behaviours that fix relies on:

  1. an EXPIRED access token 401s while its refresh token still works
     — i.e. "stale bearer" and "dead session" really are different states;
  2. a replay INSIDE the grace window gets the same pair back, so a browser's
     parallel requests can't kill a healthy session by racing each other;
  3. a replay OUTSIDE the window still 401s — the theft signal survives;
  4. rotation supersedes: the new refresh token works, the old one is spent;
  5. an expired refresh token is still rejected, so sessions aren't immortal.
"""
import time
import uuid

import jwt as _jwt
from httpx import AsyncClient

from src.api.auth import _ALGORITHM, _ISSUER, _REFRESH_AUDIENCE
from src.config import settings


async def _register(client: AsyncClient, label: str) -> tuple[dict, str]:
    """Register a throwaway user. The address is unique per run so a reused test
    database can't fail the registration with "account already exists"."""
    email = f"{label}_{uuid.uuid4().hex[:10]}@test.bridgeleads.io"
    resp = await client.post(
        "/auth/register",
        json={
            "first_name": "Test",
            "last_name": "User",
            "email": email,
            "password": "SecurePass1!",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json(), email


def _jti_of(refresh_token: str) -> str:
    return _jwt.decode(refresh_token, options={"verify_signature": False})["jti"]


async def _lapse_grace(refresh_token: str) -> None:
    """Drop the replay-grace entry, i.e. fast-forward past the window."""
    from src.api.middleware.auth_hardening import TokenBlacklist, _get_redis

    await _get_redis().delete(
        f"{TokenBlacklist._REPLAY_PREFIX}{_jti_of(refresh_token)}"
    )


async def test_expired_access_token_401s_but_refresh_still_recovers_the_session(
    client: AsyncClient,
):
    """The exact production state that produced the bug.

    An access token that has simply aged out must 401 (so the API stays safe),
    while the paired refresh token still mints a WORKING one. That gap is the
    whole reason a 401 must not be treated as a sign-out.
    """
    reg, email = await _register(client, "refresh_contract_expired")
    refresh = reg["refresh_token"]

    # Mint an access token for this same user that expired an hour ago. Same
    # signing key, issuer and audience as production — only `exp` is in the past.
    claims = _jwt.decode(
        reg["access_token"],
        settings.SECRET_KEY,
        algorithms=[_ALGORITHM],
        audience="bridgeleads-api",
        issuer=_ISSUER,
    )
    now = int(time.time())
    expired = _jwt.encode(
        {**claims, "jti": str(uuid.uuid4()), "iat": now - 7200, "exp": now - 3600},
        settings.SECRET_KEY,
        algorithm=_ALGORITHM,
    )

    # The stale bearer is rejected...
    stale = await client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert stale.status_code == 401

    # ...but the session is NOT over: the refresh token still yields a live one.
    rotated = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert rotated.status_code == 200, rotated.text
    fresh_access = rotated.json()["access_token"]

    me = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {fresh_access}"}
    )
    assert me.status_code == 200
    assert me.json()["email"] == email


async def test_replay_inside_the_grace_window_returns_the_same_pair(
    client: AsyncClient,
):
    """A replay racing inside the grace window gets the SAME pair, not a 401.

    Single-use rotation is correct but brittle for a browser: one page load fires
    many parallel requests, and several can present the same refresh token before
    the first rotation's cookie has propagated. Verified against production, two
    concurrent /auth/refresh calls return 200 and 401 — and that 401 was killing
    healthy sessions. Inside the window the loser now gets what the winner got.
    """
    reg, _ = await _register(client, "refresh_contract_single")
    refresh = reg["refresh_token"]

    first = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert first.status_code == 200, first.text

    replay = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert replay.status_code == 200, replay.text
    # Identical pair — the replay must not mint a SECOND live session.
    assert replay.json()["access_token"] == first.json()["access_token"]
    assert replay.json()["refresh_token"] == first.json()["refresh_token"]


async def test_replay_outside_the_grace_window_is_still_rejected(
    client: AsyncClient,
):
    """The grace window is bounded: once it lapses, a replay 401s again.

    This is the theft signal the window must not erase — a token resurfacing
    later is exactly what single-use rotation exists to catch.
    """
    reg, _ = await _register(client, "refresh_contract_grace")
    refresh = reg["refresh_token"]

    first = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert first.status_code == 200, first.text

    # Fast-forward past the window rather than sleeping through it.
    await _lapse_grace(refresh)

    replay = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert replay.status_code == 401
    assert "already used" in replay.json()["detail"].lower()


async def test_rotation_supersedes_the_previous_refresh_token(client: AsyncClient):
    """After rotating, the NEW refresh token works and the OLD one stays dead."""
    reg, _ = await _register(client, "refresh_contract_chain")
    original = reg["refresh_token"]

    first = await client.post("/auth/refresh", json={"refresh_token": original})
    assert first.status_code == 200, first.text
    rotated = first.json()["refresh_token"]
    assert rotated != original

    # The rotated token carries the chain forward...
    second = await client.post("/auth/refresh", json={"refresh_token": rotated})
    assert second.status_code == 200, second.text
    assert second.json()["access_token"]

    # ...and once its grace window lapses, the original is permanently spent.
    # (Inside the window a replay deliberately returns the same pair — see
    # test_replay_inside_the_grace_window_returns_the_same_pair.)
    await _lapse_grace(original)
    assert (
        await client.post("/auth/refresh", json={"refresh_token": original})
    ).status_code == 401


async def test_expired_refresh_token_is_rejected(client: AsyncClient):
    """A genuinely expired refresh token must 401 — that is a REAL sign-out.

    The fix must not make sessions immortal: when this fails, the client is
    correct to sign the user out.
    """
    now = int(time.time())
    dead = _jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "jti": str(uuid.uuid4()),
            "iss": _ISSUER,
            "aud": _REFRESH_AUDIENCE,
            "purpose": "refresh",
            "amr": ["pwd"],
            "auth_time": now - 9999,
            "iat": now - 9999,
            "exp": now - 60,
        },
        settings.SECRET_KEY,
        algorithm=_ALGORITHM,
    )
    resp = await client.post("/auth/refresh", json={"refresh_token": dead})
    assert resp.status_code == 401
