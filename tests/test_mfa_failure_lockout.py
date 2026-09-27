"""MFA verification locks after repeated wrong codes (audit 2026-09-25, A-3).

Before: the only brake on /auth/login/mfa was 10 attempts per minute per user,
with three TOTP codes valid at any moment, so an attacker who already had the
password could guess ~14,000 times a day (about a 4% daily success rate) and
never be locked out. Now five failures lock verification for that account for
15 minutes, even for a correct code, and a success clears the count.
"""
from __future__ import annotations

import uuid

from httpx import AsyncClient

from tests.test_auth import _clear_auth_limits, _register_and_enable_mfa

_PW = "SecurePass1!"


async def _challenge(client: AsyncClient, email: str) -> str:
    r = await client.post("/auth/login", json={"email": email, "password": _PW})
    assert r.status_code == 200 and r.json()["mfa_required"] is True, r.text
    return r.json()["mfa_token"]


async def test_five_wrong_codes_lock_mfa_even_for_the_right_one(client: AsyncClient, redis_client):
    email = f"mfalock_{uuid.uuid4().hex[:8]}@test.bridgeleads.io"
    _, backup_codes = await _register_and_enable_mfa(client, redis_client, email, _PW)
    token = await _challenge(client, email)

    for _ in range(5):
        wrong = await client.post("/auth/login/mfa", json={"mfa_token": token, "code": "000000"})
        assert wrong.status_code == 401

    _clear_auth_limits(redis_client)  # the IP/minute limiter is not what is under test
    locked = await client.post("/auth/login/mfa", json={"mfa_token": token, "code": backup_codes[0]})
    assert locked.status_code == 429, locked.text
    assert "Retry-After" in locked.headers


async def test_mfa_disable_is_not_a_second_place_to_guess_codes(client: AsyncClient, redis_client):
    """Disabling MFA also checks a code; it shares the same per-account lock."""
    email = f"mfadis_{uuid.uuid4().hex[:8]}@test.bridgeleads.io"
    _, backup_codes = await _register_and_enable_mfa(client, redis_client, email, _PW)
    token = await _challenge(client, email)
    session = await client.post("/auth/login/mfa", json={"mfa_token": token, "code": backup_codes[0]})
    auth = {"Authorization": f"Bearer {session.json()['access_token']}"}

    for _ in range(5):
        _clear_auth_limits(redis_client)
        wrong = await client.post("/auth/mfa/disable", headers=auth, json={"password": _PW, "code": "000000"})
        assert wrong.status_code == 400
    _clear_auth_limits(redis_client)
    locked = await client.post(
        "/auth/mfa/disable", headers=auth, json={"password": _PW, "code": backup_codes[1]}
    )
    assert locked.status_code == 429, locked.text


async def test_a_success_clears_the_failure_count(client: AsyncClient, redis_client):
    email = f"mfaok_{uuid.uuid4().hex[:8]}@test.bridgeleads.io"
    _, backup_codes = await _register_and_enable_mfa(client, redis_client, email, _PW)

    token = await _challenge(client, email)
    for _ in range(4):
        assert (await client.post("/auth/login/mfa", json={"mfa_token": token, "code": "000000"})).status_code == 401
    ok = await client.post("/auth/login/mfa", json={"mfa_token": token, "code": backup_codes[0]})
    assert ok.status_code == 200, ok.text

    # Four more failures after the success must not lock: the count restarted.
    _clear_auth_limits(redis_client)
    token = await _challenge(client, email)
    for _ in range(4):
        assert (await client.post("/auth/login/mfa", json={"mfa_token": token, "code": "000000"})).status_code == 401
    ok = await client.post("/auth/login/mfa", json={"mfa_token": token, "code": backup_codes[1]})
    assert ok.status_code == 200, ok.text
