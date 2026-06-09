"""H2-P5 Step C: break-glass code generation (pure-function, no DB).

Redemption-flow tests that need Postgres/Redis live in the integration suite;
these cover the operator-side code generator + its hashing contract, which is
what the redeem path verifies against.
"""

from src.utils.mfa import (
    _normalize_backup_code,
    generate_break_glass_codes,
    hash_backup_code,
)


def test_generates_requested_count():
    pt, h = generate_break_glass_codes(8)
    assert len(pt) == 8
    assert len(h) == 8


def test_codes_are_unique():
    pt, h = generate_break_glass_codes(12)
    assert len(set(pt)) == 12  # plaintext unique
    assert len(set(h)) == 12   # hashes unique


def test_codes_have_bg_prefix_and_are_distinct_from_backup_codes():
    pt, _ = generate_break_glass_codes(5)
    assert all(c.startswith("bg-") for c in pt)


def test_stored_hash_matches_recomputed_hash():
    """The redeem path looks codes up by hash_backup_code(input); the stored hash
    must therefore equal hash_backup_code(plaintext)."""
    pt, h = generate_break_glass_codes(6)
    assert all(hash_backup_code(c) == stored for c, stored in zip(pt, h, strict=True))


def test_hash_is_normalization_insensitive():
    """User formatting (case, dashes, spaces) must not change the verified hash —
    the redeem UPDATE matches on the normalized HMAC digest."""
    pt, h = generate_break_glass_codes(1)
    code = pt[0]
    upper_spaced = code.upper().replace("-", " ")
    assert hash_backup_code(upper_spaced) == h[0]
    # sanity: normalization actually collapses the variants
    assert _normalize_backup_code(upper_spaced) == _normalize_backup_code(code)


def test_high_entropy_length():
    """128-bit codes -> 26 base32 chars after the 'bg-' prefix (dashes cosmetic)."""
    pt, _ = generate_break_glass_codes(1)
    stripped = _normalize_backup_code(pt[0])  # removes 'bg' prefix? no: keeps 'bg'
    # 'bg' (2) + 26 base32 chars = 28 normalized chars
    assert len(stripped) == 28
