"""The per-process fallback limiter must not be wipeable.

During a Redis outage this is the ONLY limiter protecting the auth, webhook and
stripe zones. The previous implementation called `_fallback_hits.clear()` once
the dict passed 10k keys, so an attacker could mint throwaway keys and erase
every real counter — including the brute-force ladder on an account they were
attacking. Reclaiming space must never discard an ACTIVE counter.
"""
from src.api.middleware.rate_limit import (
    _FALLBACK_MAX_KEYS,
    _fallback_allow,
    _fallback_hits,
)


def _reset() -> None:
    _fallback_hits.clear()


def test_flood_of_throwaway_keys_cannot_reset_an_active_counter():
    _reset()
    now = 1000.0
    for _ in range(5):
        _fallback_allow("victim", 5, 60, now)
    assert _fallback_allow("victim", 5, 60, now) is False, "victim should be limited"

    # Attacker pushes the map well past its bound with distinct keys.
    for i in range(_FALLBACK_MAX_KEYS + 500):
        _fallback_allow(f"flood-{i}", 5, 60, now)

    assert _fallback_allow("victim", 5, 60, now) is False, (
        "the victim's counter was reset by an unrelated key flood — the "
        "wholesale-clear bypass is back"
    )
    _reset()


def test_memory_stays_bounded_under_a_flood():
    _reset()
    now = 2000.0
    for i in range(_FALLBACK_MAX_KEYS * 2):
        _fallback_allow(f"k-{i}", 5, 60, now)
    assert len(_fallback_hits) <= _FALLBACK_MAX_KEYS + 2, (
        f"fallback map grew unbounded: {len(_fallback_hits)}"
    )
    _reset()


def test_expired_buckets_are_reclaimed_so_normal_traffic_is_not_denied():
    _reset()
    now = 3000.0
    for i in range(_FALLBACK_MAX_KEYS + 100):
        _fallback_allow(f"old-{i}", 5, 60, now)
    # Well past the window: those buckets are expired and must be reclaimable,
    # so a legitimate new caller is still admitted rather than failing closed
    # forever after one historical flood.
    later = now + 3600
    assert _fallback_allow("fresh-caller", 5, 60, later) is True
    _reset()
