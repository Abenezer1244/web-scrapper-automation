"""Tests for authentication: register, login, JWT, brute-force, API keys."""
import asyncio
import time
import uuid

import pyotp
from httpx import AsyncClient

from src.db.models import User
from src.utils.mfa import _TOTP_INTERVAL

# ─── Register ─────────────────────────────────────────────────────────────────

async def test_register_creates_user(client: AsyncClient):
    resp = await client.post("/auth/register", json={"first_name": "Test", "last_name": "User",
        "email": "newuser@test.bridgeleads.io",
        "password": "SecurePass1!",
    })
    assert resp.status_code == 201
    data = resp.json()
    # Register returns TokenResponse (access_token + token_type)
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert "password" not in data
    assert "password_hash" not in data

    # Verify user was created with correct fields via /auth/me
    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {data['access_token']}"})
    assert me.status_code == 200
    me_data = me.json()
    assert me_data["email"] == "newuser@test.bridgeleads.io"
    assert me_data["plan"] == "pro"  # registration defaults new users to pro (auth.py)


async def test_register_duplicate_returns_generic_error(client: AsyncClient):
    payload = {"first_name": "Test", "last_name": "User", "email": "dup@test.bridgeleads.io", "password": "SecurePass1!"}
    await client.post("/auth/register", json=payload)
    resp = await client.post("/auth/register", json=payload)
    # Must be 400 with a generic message — never reveal "already exists"
    assert resp.status_code == 400
    assert "already" not in resp.json()["detail"].lower()


async def test_register_short_password_rejected(client: AsyncClient):
    resp = await client.post("/auth/register", json={"first_name": "Test", "last_name": "User",
        "email": "short@test.bridgeleads.io",
        "password": "abc",
    })
    assert resp.status_code == 422


async def test_register_invalid_email_rejected(client: AsyncClient):
    resp = await client.post("/auth/register", json={"first_name": "Test", "last_name": "User",
        "email": "not-an-email",
        "password": "SecurePass1!",
    })
    assert resp.status_code == 422


# ─── Login ────────────────────────────────────────────────────────────────────

async def test_login_returns_token(client: AsyncClient):
    await client.post("/auth/register", json={"first_name": "Test", "last_name": "User",
        "email": "login_ok@test.bridgeleads.io",
        "password": "SecurePass1!",
    })
    resp = await client.post("/auth/login", json={
        "email": "login_ok@test.bridgeleads.io",
        "password": "SecurePass1!",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


async def test_login_wrong_password_generic_error(client: AsyncClient):
    await client.post("/auth/register", json={"first_name": "Test", "last_name": "User",
        "email": "wrongpass@test.bridgeleads.io",
        "password": "SecurePass1!",
    })
    resp = await client.post("/auth/login", json={
        "email": "wrongpass@test.bridgeleads.io",
        "password": "WrongPassword!",
    })
    assert resp.status_code == 401
    # Generic message — must not distinguish between bad email vs bad password
    assert "invalid" in resp.json()["detail"].lower()


async def test_login_unknown_email_same_generic_error(client: AsyncClient):
    resp = await client.post("/auth/login", json={
        "email": "ghost@test.bridgeleads.io",
        "password": "SomePass1!",
    })
    assert resp.status_code == 401
    assert "invalid" in resp.json()["detail"].lower()


# ─── Refresh-token isolation (CRITICAL: refresh tokens must NOT be access tokens)

async def test_refresh_token_cannot_authenticate_requests(client: AsyncClient):
    reg = await client.post("/auth/register", json={"first_name": "Test", "last_name": "User",
        "email": "refresh_iso@test.bridgeleads.io", "password": "SecurePass1!",
    })
    refresh = reg.json()["refresh_token"]
    assert refresh
    # A 7-day refresh token must be useless as a bearer on a normal endpoint.
    resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {refresh}"})
    assert resp.status_code == 401


async def test_refresh_endpoint_rotates_and_new_access_works(client: AsyncClient):
    reg = await client.post("/auth/register", json={"first_name": "Test", "last_name": "User",
        "email": "refresh_rot@test.bridgeleads.io", "password": "SecurePass1!",
    })
    r = await client.post("/auth/refresh", json={"refresh_token": reg.json()["refresh_token"]})
    assert r.status_code == 200, r.text
    data = r.json()
    # New access token works...
    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {data['access_token']}"})
    assert me.status_code == 200
    assert me.json()["email"] == "refresh_rot@test.bridgeleads.io"
    # ...but the new refresh token still can't authenticate.
    bad = await client.get("/auth/me", headers={"Authorization": f"Bearer {data['refresh_token']}"})
    assert bad.status_code == 401


async def test_access_token_cannot_be_used_to_refresh(client: AsyncClient):
    reg = await client.post("/auth/register", json={"first_name": "Test", "last_name": "User",
        "email": "refresh_xacc@test.bridgeleads.io", "password": "SecurePass1!",
    })
    r = await client.post("/auth/refresh", json={"refresh_token": reg.json()["access_token"]})
    assert r.status_code == 401


async def test_legacy_refresh_token_rejected_as_access(client: AsyncClient):
    # A refresh token minted under the OLD shared audience (aud=bridgeleads-api,
    # purpose=refresh) passes the access-audience pin, so the purpose BELT in
    # get_current_user is what must reject it — this is the 7-day migration risk
    # (already-issued refresh tokens authenticating) the fix neutralizes.
    import jwt as _jwt

    from src.api.auth import _ACCESS_AUDIENCE, _ALGORITHM, _ISSUER
    from src.config import settings

    now = int(time.time())
    legacy = _jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "jti": str(uuid.uuid4()),
            "iss": _ISSUER,
            "aud": _ACCESS_AUDIENCE,  # old shared audience
            "purpose": "refresh",
            "iat": now,
            "exp": now + 3600,
        },
        settings.SECRET_KEY,
        algorithm=_ALGORITHM,
    )
    resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {legacy}"})
    assert resp.status_code == 401


# ─── /auth/me ─────────────────────────────────────────────────────────────────

async def test_get_me_with_valid_token(client: AsyncClient, starter_user: User, starter_token: str):
    resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {starter_token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == starter_user.email


async def test_get_me_without_token_returns_401(client: AsyncClient):
    resp = await client.get("/auth/me")
    assert resp.status_code == 401


async def test_get_me_with_invalid_token_returns_401(client: AsyncClient):
    resp = await client.get("/auth/me", headers={"Authorization": "Bearer notavalidtoken"})
    assert resp.status_code == 401


# ─── Logout / JWT blacklist ───────────────────────────────────────────────────

async def test_logout_blacklists_token(client: AsyncClient):
    # Register + login to get a fresh token
    await client.post("/auth/register", json={"first_name": "Test", "last_name": "User",
        "email": "logout_test@test.bridgeleads.io",
        "password": "SecurePass1!",
    })
    login = await client.post("/auth/login", json={
        "email": "logout_test@test.bridgeleads.io",
        "password": "SecurePass1!",
    })
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Token works before logout
    assert (await client.get("/auth/me", headers=headers)).status_code == 200

    # Logout (returns 204 No Content)
    assert (await client.post("/auth/logout", headers=headers)).status_code == 204

    # Token rejected after logout
    resp = await client.get("/auth/me", headers=headers)
    assert resp.status_code == 401


# ─── Brute-force protection ───────────────────────────────────────────────────

async def test_brute_force_lockout_after_five_failures(client: AsyncClient, redis_client):
    email = "brute@test.bridgeleads.io"
    await client.post("/auth/register", json={"first_name": "Test", "last_name": "User", "email": email, "password": "SecurePass1!"})

    # Clear any existing brute-force state for this email
    for key in redis_client.scan_iter(f"bf:email:{email}*"):
        redis_client.delete(key)

    # 5 failed attempts
    for _ in range(5):
        await client.post("/auth/login", json={"email": email, "password": "WrongPass1!"})

    # 6th attempt must be rate-limited
    resp = await client.post("/auth/login", json={"email": email, "password": "WrongPass1!"})
    assert resp.status_code == 429


# ─── API key (Business+ only) ─────────────────────────────────────────────────

async def test_api_key_generation_business_user(client: AsyncClient, business_user: User, business_token: str):
    resp = await client.post(
        "/auth/api-key",
        headers={"Authorization": f"Bearer {business_token}"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "api_key" in data
    assert data["api_key"].startswith("bl_")


async def test_api_key_rejected_for_starter(client: AsyncClient, starter_user: User, starter_token: str):
    resp = await client.post(
        "/auth/api-key",
        headers={"Authorization": f"Bearer {starter_token}"},
    )
    assert resp.status_code == 403


async def test_api_key_authenticates_requests(client: AsyncClient, business_user: User, business_token: str):
    # Generate API key
    key_resp = await client.post(
        "/auth/api-key",
        headers={"Authorization": f"Bearer {business_token}"},
    )
    api_key = key_resp.json()["api_key"]

    # Use API key to authenticate
    resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == business_user.email


# ─── MFA login challenge (H2-P3) ──────────────────────────────────────────────
# Real end-to-end: enrollment goes through the public /auth/mfa/* endpoints with
# codes computed by pyotp (no mocks). Login is exercised as the two-step
# challenge → verify flow the frontend drives.

def _mfa_email() -> str:
    return f"mfa_{uuid.uuid4().hex[:8]}@test.bridgeleads.io"


def _clear_auth_limits(redis_client) -> None:
    """Reset per-IP/per-user auth rate-limit + brute-force counters. The suite
    shares a single test-client IP, so without this the auth zone (10/min) and
    brute-force buckets accumulate across MFA tests and cause spurious 429s."""
    for pattern in ("rl:auth:*", "bf:*"):
        for key in redis_client.scan_iter(pattern):
            redis_client.delete(key)


async def _register_and_enable_mfa(client: AsyncClient, redis_client, email: str,
                                   password: str = "SecurePass1!") -> tuple[str, list[str]]:
    """Enroll MFA for a fresh user via the real endpoints. Returns
    (totp_secret, backup_codes)."""
    _clear_auth_limits(redis_client)
    reg = await client.post("/auth/register", json={"first_name": "Test", "last_name": "User", "email": email, "password": password})
    assert reg.status_code == 201, reg.text
    headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}

    setup = await client.post("/auth/mfa/setup", headers=headers)
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]

    enable = await client.post(
        "/auth/mfa/enable", headers=headers, json={"code": pyotp.TOTP(secret).now()},
    )
    assert enable.status_code == 200, enable.text
    backup_codes = enable.json()["backup_codes"]
    # mfa/enable revokes all sessions at whole-second precision. Wait for the next
    # second so a subsequent login's challenge iat > revoke_time — otherwise the
    # issued_at <= revoke_time check rejects it ("Invalid or expired MFA challenge").
    _t = int(time.time())
    while int(time.time()) <= _t:
        await asyncio.sleep(0.05)
    return secret, backup_codes


async def test_login_without_mfa_has_no_challenge(client: AsyncClient, redis_client):
    _clear_auth_limits(redis_client)
    email = _mfa_email()
    await client.post("/auth/register", json={"first_name": "Test", "last_name": "User", "email": email, "password": "SecurePass1!"})
    resp = await client.post("/auth/login", json={"email": email, "password": "SecurePass1!"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["access_token"]
    assert data["mfa_required"] is False
    assert data["mfa_token"] is None


async def test_login_with_mfa_returns_challenge(client: AsyncClient, redis_client):
    email = _mfa_email()
    await _register_and_enable_mfa(client, redis_client, email)
    _clear_auth_limits(redis_client)

    resp = await client.post("/auth/login", json={"email": email, "password": "SecurePass1!"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["mfa_required"] is True
    assert data["mfa_token"]
    assert data["access_token"] is None  # no session until the 2nd factor passes

    # The challenge token must NOT authenticate an API request (distinct audience).
    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {data['mfa_token']}"})
    assert me.status_code == 401


async def test_login_mfa_totp_completes_login(client: AsyncClient, redis_client):
    email = _mfa_email()
    secret, _ = await _register_and_enable_mfa(client, redis_client, email)
    _clear_auth_limits(redis_client)

    challenge = await client.post("/auth/login", json={"email": email, "password": "SecurePass1!"})
    mfa_token = challenge.json()["mfa_token"]

    # Use a code NEWER than the enrollment-seeded counter (H2-P4): the exact
    # enrollment code is rejected as a replay, so the first login uses counter+1.
    code = pyotp.TOTP(secret).at((int(time.time()) // _TOTP_INTERVAL + 1) * _TOTP_INTERVAL)
    resp = await client.post(
        "/auth/login/mfa", json={"mfa_token": mfa_token, "code": code},
    )
    assert resp.status_code == 200, resp.text
    access = resp.json()["access_token"]
    assert access
    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert me.status_code == 200
    assert me.json()["email"] == email


async def test_login_mfa_wrong_code_rejected(client: AsyncClient, redis_client):
    email = _mfa_email()
    await _register_and_enable_mfa(client, redis_client, email)
    _clear_auth_limits(redis_client)

    challenge = await client.post("/auth/login", json={"email": email, "password": "SecurePass1!"})
    resp = await client.post(
        "/auth/login/mfa", json={"mfa_token": challenge.json()["mfa_token"], "code": "000000"},
    )
    assert resp.status_code == 401
    assert "code" in resp.json()["detail"].lower()


async def test_login_mfa_backup_code_single_use(client: AsyncClient, redis_client):
    email = _mfa_email()
    _, backup_codes = await _register_and_enable_mfa(client, redis_client, email)
    code = backup_codes[0]

    # First use of the backup code completes login.
    _clear_auth_limits(redis_client)
    ch1 = await client.post("/auth/login", json={"email": email, "password": "SecurePass1!"})
    r1 = await client.post("/auth/login/mfa", json={"mfa_token": ch1.json()["mfa_token"], "code": code})
    assert r1.status_code == 200, r1.text
    assert r1.json()["access_token"]

    # Re-using the SAME backup code must fail — it was consumed atomically.
    _clear_auth_limits(redis_client)
    ch2 = await client.post("/auth/login", json={"email": email, "password": "SecurePass1!"})
    r2 = await client.post("/auth/login/mfa", json={"mfa_token": ch2.json()["mfa_token"], "code": code})
    assert r2.status_code == 401


async def test_login_mfa_backup_code_concurrent_single_use(client: AsyncClient, redis_client):
    # Codex H2-P3: the atomic conditional UPDATE must let exactly ONE of two
    # concurrent redemptions of the same backup code win. Each request carries a
    # DISTINCT challenge token (different jti) so the race is purely on the
    # backup-code row, not the replay gate. Each request gets its own DB session
    # / connection, so this exercises a real two-transaction race.
    email = _mfa_email()
    _, backup_codes = await _register_and_enable_mfa(client, redis_client, email)
    code = backup_codes[0]

    _clear_auth_limits(redis_client)
    ch_a = await client.post("/auth/login", json={"email": email, "password": "SecurePass1!"})
    ch_b = await client.post("/auth/login", json={"email": email, "password": "SecurePass1!"})

    r_a, r_b = await asyncio.gather(
        client.post("/auth/login/mfa", json={"mfa_token": ch_a.json()["mfa_token"], "code": code}),
        client.post("/auth/login/mfa", json={"mfa_token": ch_b.json()["mfa_token"], "code": code}),
    )
    statuses = sorted([r_a.status_code, r_b.status_code])
    # Exactly one success, one rejection — never two sessions from one code.
    assert statuses == [200, 401], statuses


async def test_login_mfa_invalid_challenge_token_rejected(client: AsyncClient, redis_client):
    _clear_auth_limits(redis_client)
    resp = await client.post("/auth/login/mfa", json={"mfa_token": "not.a.jwt", "code": "123456"})
    assert resp.status_code == 401


async def test_access_token_cannot_redeem_mfa_challenge(client: AsyncClient, redis_client):
    # A session access token must not be redeemable as an MFA challenge token —
    # the two token families pin different audiences.
    _clear_auth_limits(redis_client)
    email = _mfa_email()
    reg = await client.post("/auth/register", json={"first_name": "Test", "last_name": "User", "email": email, "password": "SecurePass1!"})
    access = reg.json()["access_token"]
    resp = await client.post("/auth/login/mfa", json={"mfa_token": access, "code": "123456"})
    assert resp.status_code == 401


async def test_bad_mfa_code_does_not_lock_password_bucket(client: AsyncClient, redis_client):
    # Codex H2-P3: a wrong 2nd factor must NOT feed the password (ip,email)
    # brute-force lockout, else a password-knowing attacker could DoS-lock the
    # real user. Send MORE bad MFA attempts than the password lockout threshold
    # (5) — if they were wrongly counted, the next /auth/login would 429-lock.
    # A wrong code also must NOT consume the challenge, so one token is reused.
    email = _mfa_email()
    await _register_and_enable_mfa(client, redis_client, email)
    _clear_auth_limits(redis_client)

    challenge = await client.post("/auth/login", json={"email": email, "password": "SecurePass1!"})
    mfa_token = challenge.json()["mfa_token"]
    for _ in range(6):  # > brute-force threshold of 5 password failures
        bad = await client.post("/auth/login/mfa", json={"mfa_token": mfa_token, "code": "000000"})
        assert bad.status_code == 401

    # Password path must still be open: a challenge is issued, not a 429 lockout.
    again = await client.post("/auth/login", json={"email": email, "password": "SecurePass1!"})
    assert again.status_code == 200
    assert again.json()["mfa_required"] is True


# ─── TOTP replay prevention (H2-P4) ───────────────────────────────────────────
# Codes are computed for SPECIFIC 30s counters (not pyotp .now()) so the tests
# are deterministic across 30s boundaries. Enrollment seeds mfa_last_totp_counter
# to the enrollment code's counter (≤ now), so a code for counter now+1 is always
# strictly-newer AND inside pyotp's ±1 window.

async def _verify_mfa(client: AsyncClient, redis_client, email: str, code: str,
                      password: str = "SecurePass1!"):
    """Fresh challenge → redeem `code` at /auth/login/mfa. Returns the response."""
    _clear_auth_limits(redis_client)
    challenge = await client.post("/auth/login", json={"email": email, "password": password})
    return await client.post(
        "/auth/login/mfa",
        json={"mfa_token": challenge.json()["mfa_token"], "code": code},
    )


async def test_login_mfa_totp_replay_rejected(client: AsyncClient, redis_client):
    email = _mfa_email()
    secret, _ = await _register_and_enable_mfa(client, redis_client, email)
    totp = pyotp.TOTP(secret)
    # Counter strictly newer than the seeded enrollment counter, still in-window.
    code = totp.at((int(time.time()) // _TOTP_INTERVAL + 1) * _TOTP_INTERVAL)

    first = await _verify_mfa(client, redis_client, email, code)
    assert first.status_code == 200, first.text
    assert first.json()["access_token"]

    # Same code again → replay; rejected (and NOT treated as a backup code).
    replay = await _verify_mfa(client, redis_client, email, code)
    assert replay.status_code == 401


async def test_login_mfa_totp_concurrent_single_use(client: AsyncClient, redis_client):
    # Two concurrent redemptions of the SAME TOTP code (distinct challenges) —
    # the atomic counter advance must let exactly one win.
    email = _mfa_email()
    secret, _ = await _register_and_enable_mfa(client, redis_client, email)
    totp = pyotp.TOTP(secret)
    code = totp.at((int(time.time()) // _TOTP_INTERVAL + 1) * _TOTP_INTERVAL)

    _clear_auth_limits(redis_client)
    ch_a = await client.post("/auth/login", json={"email": email, "password": "SecurePass1!"})
    ch_b = await client.post("/auth/login", json={"email": email, "password": "SecurePass1!"})
    r_a, r_b = await asyncio.gather(
        client.post("/auth/login/mfa", json={"mfa_token": ch_a.json()["mfa_token"], "code": code}),
        client.post("/auth/login/mfa", json={"mfa_token": ch_b.json()["mfa_token"], "code": code}),
    )
    assert sorted([r_a.status_code, r_b.status_code]) == [200, 401]


async def test_enrollment_totp_code_cannot_be_replayed_at_login(client: AsyncClient, redis_client):
    # The exact TOTP code used to ENABLE MFA must not also work for the first
    # login — enable seeds mfa_last_totp_counter with that code's counter.
    email = _mfa_email()
    _clear_auth_limits(redis_client)
    reg = await client.post("/auth/register", json={"first_name": "Test", "last_name": "User", "email": email, "password": "SecurePass1!"})
    headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    secret = (await client.post("/auth/mfa/setup", headers=headers)).json()["secret"]

    enroll_counter = int(time.time()) // _TOTP_INTERVAL
    enroll_code = pyotp.TOTP(secret).at(enroll_counter * _TOTP_INTERVAL)
    enable = await client.post("/auth/mfa/enable", headers=headers, json={"code": enroll_code})
    assert enable.status_code == 200, enable.text

    # Same enrollment code at login → rejected (replay of the seeded counter, or
    # out-of-window — either way never a valid login).
    resp = await _verify_mfa(client, redis_client, email, enroll_code)
    assert resp.status_code == 401
