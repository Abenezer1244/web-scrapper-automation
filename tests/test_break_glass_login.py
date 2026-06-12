"""H2-P5 Step C2: in-app break-glass redemption (/auth/login/break-glass).

Real-flow integration tests. Like the rest of test_auth.py these need Postgres +
Redis and the `client`/`db`/`redis_client` fixtures, so they run in CI against a
dedicated test DB — NOT locally (the only configured DATABASE_URL is production,
which the db fixture table-wipes).

They prove the recovery-only contract: redeeming a break-glass code clears MFA,
revokes existing sessions, mints a degraded session, and is single-use; the
resulting session can never perform a sensitive admin op.
"""
import asyncio
import time
import uuid

import pyotp
from httpx import AsyncClient
from sqlalchemy import select

from src.db.models import MfaBackupCode, MfaBreakGlassCode, User
from src.utils.crypto import blind_index
from src.utils.mfa import _TOTP_INTERVAL, generate_break_glass_codes, hash_backup_code

_PW = "SecurePass1!"


def _email() -> str:
    return f"bg_{uuid.uuid4().hex[:8]}@test.bridgeleads.io"


def _clear_auth_limits(redis_client) -> None:
    for pattern in ("rl:auth:*", "bf:*"):
        for key in redis_client.scan_iter(pattern):
            redis_client.delete(key)


async def _register_and_enable_mfa(client: AsyncClient, redis_client, email: str) -> str:
    _clear_auth_limits(redis_client)
    reg = await client.post("/auth/register", json={"email": email, "password": _PW})
    assert reg.status_code == 201, reg.text
    headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}
    setup = await client.post("/auth/mfa/setup", headers=headers)
    secret = setup.json()["secret"]
    enable = await client.post(
        "/auth/mfa/enable", headers=headers, json={"code": pyotp.TOTP(secret).now()},
    )
    assert enable.status_code == 200, enable.text
    # mfa/enable revokes all sessions at whole-second precision. Wait for the next
    # second so a subsequent login's challenge iat > revoke_time — otherwise the
    # issued_at <= revoke_time check rejects it ("Invalid or expired MFA challenge").
    _t = int(time.time())
    while int(time.time()) <= _t:
        await asyncio.sleep(0.05)
    return secret


async def _seed_break_glass(db, user_id: str, n: int = 3, **overrides) -> list[str]:
    """Insert n break-glass codes for user_id (mimics generate_break_glass.py)."""
    plaintext, hashes = generate_break_glass_codes(n)
    batch = str(uuid.uuid4())
    for h in hashes:
        db.add(
            MfaBreakGlassCode(
                id=str(uuid.uuid4()),
                user_id=user_id,
                code_hash=h,
                batch_id=batch,
                created_by="test-operator",
                created_reason="integration test",
                **overrides,
            )
        )
    await db.commit()
    return plaintext


async def _user_id(db, email: str) -> str:
    # H3: email is encrypted at rest — match on the blind index, not the value.
    return (
        await db.execute(select(User).where(User.email_hmac == blind_index(email)))
    ).scalar_one().id


async def _full_mfa_login(client: AsyncClient, redis_client, email: str, secret: str) -> str:
    """Complete a real TOTP login and return the access token."""
    _clear_auth_limits(redis_client)
    ch = await client.post("/auth/login", json={"email": email, "password": _PW})
    mfa_token = ch.json()["mfa_token"]
    import time
    code = pyotp.TOTP(secret).at((int(time.time()) // _TOTP_INTERVAL + 1) * _TOTP_INTERVAL)
    r = await client.post("/auth/login/mfa", json={"mfa_token": mfa_token, "code": code})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _challenge(client: AsyncClient, redis_client, email: str) -> str:
    _clear_auth_limits(redis_client)
    ch = await client.post("/auth/login", json={"email": email, "password": _PW})
    assert ch.json()["mfa_required"] is True
    return ch.json()["mfa_token"]


# ─── happy path ───────────────────────────────────────────────────────────────

async def test_break_glass_redeem_clears_mfa_and_logs_in(client, db, redis_client):
    email = _email()
    await _register_and_enable_mfa(client, redis_client, email)
    uid = await _user_id(db, email)
    codes = await _seed_break_glass(db, uid)

    mfa_token = await _challenge(client, redis_client, email)
    r = await client.post(
        "/auth/login/break-glass", json={"mfa_token": mfa_token, "code": codes[0]},
    )
    assert r.status_code == 200, r.text
    access = r.json()["access_token"]
    assert access

    # The recovery session works as a bearer.
    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert me.status_code == 200
    assert me.json()["email"] == email

    # MFA is now cleared — a fresh login no longer issues a challenge.
    _clear_auth_limits(redis_client)
    login = await client.post("/auth/login", json={"email": email, "password": _PW})
    assert login.json()["mfa_required"] is False
    assert login.json()["access_token"]


async def test_break_glass_revokes_prior_sessions_and_new_one_survives(client, db, redis_client):
    email = _email()
    secret = await _register_and_enable_mfa(client, redis_client, email)
    uid = await _user_id(db, email)
    codes = await _seed_break_glass(db, uid)

    # A real MFA session that exists BEFORE the break-glass.
    old_access = await _full_mfa_login(client, redis_client, email, secret)
    assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {old_access}"})).status_code == 200

    mfa_token = await _challenge(client, redis_client, email)
    r = await client.post(
        "/auth/login/break-glass", json={"mfa_token": mfa_token, "code": codes[0]},
    )
    assert r.status_code == 200, r.text
    new_access = r.json()["access_token"]

    # Old session is revoked; the new recovery session (minted in the same
    # request as the revoke) survives — proves the now-1s revoke shift.
    assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {old_access}"})).status_code == 401
    assert (await client.get("/auth/me", headers={"Authorization": f"Bearer {new_access}"})).status_code == 200


# ─── rejection paths ──────────────────────────────────────────────────────────

async def test_break_glass_wrong_code_rejected_mfa_intact(client, db, redis_client):
    email = _email()
    await _register_and_enable_mfa(client, redis_client, email)
    uid = await _user_id(db, email)
    await _seed_break_glass(db, uid)

    mfa_token = await _challenge(client, redis_client, email)
    r = await client.post(
        "/auth/login/break-glass", json={"mfa_token": mfa_token, "code": "bg-0000-0000-0000-0000"},
    )
    assert r.status_code == 401
    # MFA must still be enabled — a wrong code changes nothing.
    _clear_auth_limits(redis_client)
    login = await client.post("/auth/login", json={"email": email, "password": _PW})
    assert login.json()["mfa_required"] is True


async def test_break_glass_used_code_rejected(client, db, redis_client):
    email = _email()
    await _register_and_enable_mfa(client, redis_client, email)
    uid = await _user_id(db, email)
    # Seed a single ALREADY-USED code.
    from datetime import UTC, datetime
    codes = await _seed_break_glass(db, uid, n=1, used_at=datetime.now(UTC))

    mfa_token = await _challenge(client, redis_client, email)
    r = await client.post(
        "/auth/login/break-glass", json={"mfa_token": mfa_token, "code": codes[0]},
    )
    assert r.status_code == 401


async def test_break_glass_challenge_replay_rejected(client, db, redis_client):
    email = _email()
    await _register_and_enable_mfa(client, redis_client, email)
    uid = await _user_id(db, email)
    codes = await _seed_break_glass(db, uid)

    mfa_token = await _challenge(client, redis_client, email)
    r1 = await client.post(
        "/auth/login/break-glass", json={"mfa_token": mfa_token, "code": codes[0]},
    )
    assert r1.status_code == 200, r1.text
    # Replaying the same challenge token (even with another seeded code) fails:
    # jti burned + MFA now cleared.
    r2 = await client.post(
        "/auth/login/break-glass", json={"mfa_token": mfa_token, "code": codes[1]},
    )
    assert r2.status_code == 401


# ─── recovery-only: the resulting session cannot do admin ops ─────────────────

async def test_break_glass_session_cannot_reach_admin_op(client, db, redis_client):
    email = _email()
    await _register_and_enable_mfa(client, redis_client, email)
    uid = await _user_id(db, email)
    # Make this user an admin so the admin gate is reached on the merits.
    user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
    user.is_admin = True
    await db.commit()
    codes = await _seed_break_glass(db, uid)

    mfa_token = await _challenge(client, redis_client, email)
    r = await client.post(
        "/auth/login/break-glass", json={"mfa_token": mfa_token, "code": codes[0]},
    )
    access = r.json()["access_token"]
    hdr = {"Authorization": f"Bearer {access}"}

    # Connector creation requires admin step-up; the break-glass session has no
    # "mfa" amr AND mfa is now disabled, so the admin gate rejects it. (403
    # enrollment-required because mfa_enabled was torn down by the recovery.)
    resp = await client.post(
        "/scrapers/connectors",
        headers=hdr,
        json={
            "county": "testcounty",
            "state": "WA",
            "record_types": ["probate"],
            "base_url": "https://example.com",
        },
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] in (
        "admin_mfa_enrollment_required",
        "admin_mfa_step_up_required",
    )


# ─── side effects: all recovery credentials burned ────────────────────────────

async def test_break_glass_burns_siblings_and_backup_codes(client, db, redis_client):
    email = _email()
    await _register_and_enable_mfa(client, redis_client, email)
    uid = await _user_id(db, email)
    codes = await _seed_break_glass(db, uid, n=3)

    mfa_token = await _challenge(client, redis_client, email)
    r = await client.post(
        "/auth/login/break-glass", json={"mfa_token": mfa_token, "code": codes[0]},
    )
    assert r.status_code == 200, r.text

    # Every break-glass code is now used or revoked (none left live).
    rows = (await db.execute(select(MfaBreakGlassCode).where(MfaBreakGlassCode.user_id == uid))).scalars().all()
    assert rows
    for row in rows:
        assert row.used_at is not None or row.revoked_at is not None
    # The redeemed one is marked used (with ip), the siblings revoked.
    redeemed = [x for x in rows if x.code_hash == hash_backup_code(codes[0])]
    assert len(redeemed) == 1 and redeemed[0].used_at is not None

    # Backup codes were destroyed too.
    backups = (await db.execute(select(MfaBackupCode).where(MfaBackupCode.user_id == uid))).scalars().all()
    assert backups == []
