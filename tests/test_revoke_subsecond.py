"""Sub-second logout-all cutoff: a login right after "sign out everywhere" works,
without letting anything issued before the cutoff survive. Real DB + Redis."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth import decode_secure_token
from src.api.middleware.auth_hardening import TokenBlacklist, _epoch_ms, _get_redis
from src.db.models import User


def _token(iat_ms: int, legacy: bool = False) -> dict:
    payload = {"iat": iat_ms // 1000}
    if not legacy:
        payload["iat_ms"] = iat_ms
    return payload


@pytest.mark.asyncio
async def test_cutoff_is_millisecond_precise(starter_user: User) -> None:
    cutoff = await TokenBlacklist.revoke_all_for_user(starter_user.id)
    c = _epoch_ms(cutoff)
    revoked = TokenBlacklist.is_revoked_by_user_logout_all
    assert await revoked(starter_user.id, 0, _token(c)), "a token at the cutoff must die"
    assert await revoked(starter_user.id, 0, _token(c - 1)), "a token before the cutoff must die"
    assert not await revoked(starter_user.id, 0, _token(c + 1)), "a token 1 ms later must live"
    # Legacy tokens (no iat_ms) stay conservative: the whole revoke second is dead.
    assert await revoked(starter_user.id, 0, _token(c + 1, legacy=True))
    # Malformed issue times are never trusted.
    for bad in ({"iat": c // 1000, "iat_ms": True}, {"iat": c // 1000, "iat_ms": "x"},
                {"iat": c // 1000 + 5, "iat_ms": c + 1}, {"iat_ms": c + 1}):
        assert await revoked(starter_user.id, 0, bad), bad


@pytest.mark.asyncio
async def test_a_login_in_the_same_second_as_logout_all_works(
    client: AsyncClient, starter_user: User
) -> None:
    login = {"email": starter_user.email, "password": "TestPass123!"}
    for _ in range(5):  # several tries make a same-second login all but certain
        first = (await client.post("/auth/login", json=login)).json()
        auth = {"Authorization": f"Bearer {first['access_token']}"}
        assert (await client.post("/auth/logout-all", headers=auth)).status_code == 204
        fresh = (await client.post("/auth/login", json=login)).json()
        me = await client.get("/auth/me", headers={"Authorization": f"Bearer {fresh['access_token']}"})
        assert me.status_code == 200, "a login after logout-all was rejected"
        assert (await client.get("/auth/me", headers=auth)).status_code == 401
        assert "iat_ms" in decode_secure_token(fresh["access_token"])


@pytest.mark.asyncio
async def test_a_legacy_whole_second_cache_value_is_not_trusted(
    db: AsyncSession, starter_user: User
) -> None:
    cutoff = await TokenBlacklist.revoke_all_for_user(starter_user.id)
    # An older container's truncated value (seconds) sitting in the cache.
    await _get_redis().set(f"{TokenBlacklist._USER_REVOKE_PREFIX}{starter_user.id}",
                           str(int(cutoff.timestamp())))
    assert await TokenBlacklist.get_user_revoke_ms(starter_user.id) == _epoch_ms(cutoff)


@pytest.mark.asyncio
async def test_the_cutoff_never_moves_backward(db: AsyncSession, starter_user: User) -> None:
    later = datetime.now(UTC) + timedelta(minutes=5)  # e.g. stamped by a clock running ahead
    await db.execute(update(User).where(User.id == starter_user.id).values(revoked_at=later))
    await db.commit()
    returned = await TokenBlacklist.revoke_all_for_user(starter_user.id)
    stored = (await db.execute(
        select(User.revoked_at).where(User.id == starter_user.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert stored == later and returned == later


@pytest.mark.asyncio
async def test_an_older_cutoff_never_overwrites_a_newer_cached_one(starter_user: User) -> None:
    from src.api.middleware.auth_hardening import _raise_revoke_cache
    key = f"{TokenBlacklist._USER_REVOKE_PREFIX}{starter_user.id}"
    r = _get_redis()
    await _raise_revoke_cache(r, key, 2_000, 60)
    await _raise_revoke_cache(r, key, 1_000, 60)  # late, older writer
    assert (await r.get(key)) in (b"ms:2000", "ms:2000")
    await _raise_revoke_cache(r, key, 3_000, 60)
    assert (await r.get(key)) in (b"ms:3000", "ms:3000")


@pytest.mark.asyncio
async def test_a_legacy_cached_cutoff_is_kept_at_its_upper_bound(starter_user: User) -> None:
    from src.api.middleware.auth_hardening import _raise_revoke_cache
    key = f"{TokenBlacklist._USER_REVOKE_PREFIX}{starter_user.id}"
    r = _get_redis()
    await r.set(key, "5")  # older container: second 5, i.e. up to 5999 ms
    await _raise_revoke_cache(r, key, 5_500, 60)
    assert (await r.get(key)) in (b"ms:5999", "ms:5999")
