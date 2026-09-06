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
  2. refresh tokens are strictly SINGLE-USE
     — which is why the client must serialise refreshes rather than let a burst
       of parallel 401s each start one;
  3. rotation supersedes: the new refresh token works, the old one is dead.
"""
import time
import uuid

import jwt as _jwt
from httpx import AsyncClient

from src.api.auth import _ALGORITHM, _ISSUER, _REFRESH_AUDIENCE
from src.config import settings


async def _register(client: AsyncClient, email: str) -> dict:
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
    return resp.json()


async def test_expired_access_token_401s_but_refresh_still_recovers_the_session(
    client: AsyncClient,
):
    """The exact production state that produced the bug.

    An access token that has simply aged out must 401 (so the API stays safe),
    while the paired refresh token still mints a WORKING one. That gap is the
    whole reason a 401 must not be treated as a sign-out.
    """
    reg = await _register(client, "refresh_contract_expired@test.bridgeleads.io")
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
    assert me.json()["email"] == "refresh_contract_expired@test.bridgeleads.io"


async def test_refresh_token_is_single_use(client: AsyncClient):
    """Replaying a consumed refresh token must 401.

    This is what forces the client to serialise refreshes: if several parallel
    401s each POSTed /auth/refresh with the same token, only one would win and
    the losers would look like a dead session.
    """
    reg = await _register(client, "refresh_contract_single@test.bridgeleads.io")
    refresh = reg["refresh_token"]

    first = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert first.status_code == 200, first.text

    replay = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert replay.status_code == 401
    assert "already used" in replay.json()["detail"].lower()


async def test_rotation_supersedes_the_previous_refresh_token(client: AsyncClient):
    """After rotating, the NEW refresh token works and the OLD one stays dead."""
    reg = await _register(client, "refresh_contract_chain@test.bridgeleads.io")
    original = reg["refresh_token"]

    first = await client.post("/auth/refresh", json={"refresh_token": original})
    assert first.status_code == 200, first.text
    rotated = first.json()["refresh_token"]
    assert rotated != original

    # The rotated token carries the chain forward...
    second = await client.post("/auth/refresh", json={"refresh_token": rotated})
    assert second.status_code == 200, second.text
    assert second.json()["access_token"]

    # ...and the original remains permanently spent.
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
