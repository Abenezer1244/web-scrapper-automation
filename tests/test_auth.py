"""Tests for authentication: register, login, JWT, brute-force, API keys."""
from httpx import AsyncClient

from src.db.models import User

# ─── Register ─────────────────────────────────────────────────────────────────

async def test_register_creates_user(client: AsyncClient):
    resp = await client.post("/auth/register", json={
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
    assert me_data["plan"] == "starter"


async def test_register_duplicate_returns_generic_error(client: AsyncClient):
    payload = {"email": "dup@test.bridgeleads.io", "password": "SecurePass1!"}
    await client.post("/auth/register", json=payload)
    resp = await client.post("/auth/register", json=payload)
    # Must be 400 with a generic message — never reveal "already exists"
    assert resp.status_code == 400
    assert "already" not in resp.json()["detail"].lower()


async def test_register_short_password_rejected(client: AsyncClient):
    resp = await client.post("/auth/register", json={
        "email": "short@test.bridgeleads.io",
        "password": "abc",
    })
    assert resp.status_code == 422


async def test_register_invalid_email_rejected(client: AsyncClient):
    resp = await client.post("/auth/register", json={
        "email": "not-an-email",
        "password": "SecurePass1!",
    })
    assert resp.status_code == 422


# ─── Login ────────────────────────────────────────────────────────────────────

async def test_login_returns_token(client: AsyncClient):
    await client.post("/auth/register", json={
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
    await client.post("/auth/register", json={
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
    await client.post("/auth/register", json={
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
    await client.post("/auth/register", json={"email": email, "password": "SecurePass1!"})

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
