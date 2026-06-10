"""Per-tenant skip_trace_cache key (cross-tenant reuse removed, 2026-06-10).

Pure unit tests on address_cache_key — no DB. The security-critical invariant is
that two different tenants NEVER share a cache key for the same address, so tenant
B can't read PII tenant A paid Tracerfy to source. Same-tenant reuse (cost-saving)
and address normalization must still hold.
"""
from src.scrapers.enrichment.skip_trace import address_cache_key

_USER_A = "11111111-1111-1111-1111-111111111111"
_USER_B = "22222222-2222-2222-2222-222222222222"
_ADDR = "123 Main St"
_CITY = "Seattle"
_STATE = "WA"


def test_same_tenant_same_address_is_stable():
    """A tenant re-scraping its own address hits its own cache (reuse preserved)."""
    k1 = address_cache_key(_USER_A, _ADDR, _CITY, _STATE)
    k2 = address_cache_key(_USER_A, _ADDR, _CITY, _STATE)
    assert k1 == k2
    assert len(k1) == 64  # sha256 hexdigest


def test_different_tenants_never_collide():
    """SECURITY: same address, different tenant -> different key (no cross-tenant reuse)."""
    ka = address_cache_key(_USER_A, _ADDR, _CITY, _STATE)
    kb = address_cache_key(_USER_B, _ADDR, _CITY, _STATE)
    assert ka != kb


def test_same_tenant_different_address_differs():
    k1 = address_cache_key(_USER_A, "123 Main St", _CITY, _STATE)
    k2 = address_cache_key(_USER_A, "456 Oak Ave", _CITY, _STATE)
    assert k1 != k2


def test_normalization_still_collapses_for_one_tenant():
    """Formatting variations collapse to one key per tenant (no needless re-trace)."""
    k1 = address_cache_key(_USER_A, "123 Main St.", "Seattle", "wa")
    k2 = address_cache_key(_USER_A, "  123  main   st  ", " seattle ", "WA")
    assert k1 == k2
