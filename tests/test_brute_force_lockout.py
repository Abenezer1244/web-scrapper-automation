"""Regression tests for the brute-force lockout DURATION fix.

The bug these lock down: lockout used to be derived straight from the failure
COUNTER. A plain Redis INCR counter never decays below a threshold, so once the
count hit 5 it stayed >= 5 for the counter's whole TTL — and check() kept
returning a lockout the entire time. Result: 5 fat-fingered passwords locked an
IP for ~24h instead of the documented 1-min tier, the progressive 1/5/30-min
ladder was fiction (only the Retry-After header changed), and because check()
raises BEFORE record_failure(), a single IP froze the count at 5 so the email
NOTIFY_THRESHOLD (10) never fired.

The fix (src/api/middleware/auth_hardening.py): the COUNTER and a short-lived,
monotonic LOCK key are now separate, computed atomically in _RECORD_FAILURE_LUA.
check() consults only the lock; clear() wipes both.

Real Redis only (no mocks), per the project testing rules. The autouse
_flush_redis fixture isolates Redis state between tests; each test also uses a
distinct synthetic IP/email.
"""

import pytest
from fastapi import HTTPException

from src.api.middleware.auth_hardening import BruteForceProtection
from src.utils.crypto import blind_index


def _ip_lock(ip: str) -> str:
    return f"bf:lock:ip:{ip}"


def _ip_counter(ip: str) -> str:
    return f"bf:ip:{ip}"


def _email_lock(email: str) -> str:
    return f"bf:lock:email:{blind_index(email)}"


async def test_five_failures_arm_one_minute_lock_not_counter_ttl(redis_client):
    """5 failures -> the IP LOCK key carries the 1-min tier TTL, while the
    COUNTER key keeps its ~24h escalation memory. The whole bug was conflating
    the two: the old code would have effectively locked for the counter's TTL."""
    ip, email = "203.0.113.40", "ladder@test.bridgeleads.io"
    for _ in range(5):
        await BruteForceProtection.record_failure(ip, email)

    lock_ttl = redis_client.ttl(_ip_lock(ip))
    counter_ttl = redis_client.ttl(_ip_counter(ip))

    assert 0 < lock_ttl <= 60, f"expected 1-min tier lock, got {lock_ttl}s"
    assert counter_ttl > 3600, f"counter should keep 24h memory, got {counter_ttl}s"


async def test_check_raises_429_then_clear_releases(redis_client):
    """check() 429s while the short lock is live, and a successful login's
    clear() wipes the lock so the next attempt is allowed (the lock must not
    outlive a valid login)."""
    ip, email = "203.0.113.41", "release@test.bridgeleads.io"
    for _ in range(5):
        await BruteForceProtection.record_failure(ip, email)

    with pytest.raises(HTTPException) as exc:
        await BruteForceProtection.check(ip, email)
    assert exc.value.status_code == 429
    assert 0 < int(exc.value.headers["Retry-After"]) <= 60

    await BruteForceProtection.clear(ip, email)
    # Must not raise now.
    await BruteForceProtection.check(ip, email)
    assert redis_client.ttl(_ip_lock(ip)) < 0  # key gone (-2)


async def test_lock_ttl_is_monotonic(redis_client):
    """A later, lower-tier failure must never SHORTEN a longer active lock
    (the race Codex flagged). Pre-arm a 300s lock, then 5 failures (60s tier)
    leave it intact."""
    ip, email = "203.0.113.42", "monotonic@test.bridgeleads.io"
    redis_client.set(_ip_lock(ip), "1", ex=300)
    for _ in range(5):
        await BruteForceProtection.record_failure(ip, email)
    assert redis_client.ttl(_ip_lock(ip)) > 60


async def test_email_lock_capped_ip_lock_full_escalation(redis_client):
    """At the top tier (>=50 failures) the IP lock escalates fully (24h) while
    the EMAIL lock is capped at 15 min so an attacker spraying a victim's email
    from throwaway IPs cannot weaponise it into a long account-lockout DoS."""
    ip, email = "203.0.113.43", "cap@test.bridgeleads.io"
    for _ in range(50):
        await BruteForceProtection.record_failure(ip, email)

    ip_lock_ttl = redis_client.ttl(_ip_lock(ip))
    email_lock_ttl = redis_client.ttl(_email_lock(email))

    assert ip_lock_ttl > 15 * 60, f"IP lock should escalate above the cap, got {ip_lock_ttl}s"
    assert 0 < email_lock_ttl <= 15 * 60, f"email lock should be capped at 15min, got {email_lock_ttl}s"


def test_lockout_ladder_durations():
    """Pure ladder check — the readable mirror of the Lua computation."""
    f = BruteForceProtection._lockout_duration
    assert f(4) == 0
    assert f(5) == 60
    assert f(10) == 300
    assert f(20) == 1800
    assert f(50) == 86400
    # Email path caps at 15 min regardless of tier.
    assert f(50, is_email=True) == 15 * 60
