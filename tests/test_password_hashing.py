"""Unit tests for password hashing (src/api/auth.py).

Pure functions — no DB/network — so they validate the 2026-06-18 migration off
passlib to direct pyca/bcrypt (which unblocked bcrypt>=5) regardless of
environment. The load-bearing guarantees: existing passlib-era "$2b$12$" hashes
still verify, cost is preserved, and bcrypt 5's >72-byte ValueError can't crash
auth.
"""
from src.api.auth import BCRYPT_ROUNDS, hash_password, verify_password


def test_hash_verify_round_trip():
    h = hash_password("TestPass123!")
    assert h.startswith("$2b$")
    assert verify_password("TestPass123!", h) is True
    assert verify_password("wrong-password", h) is False


def test_cost_factor_preserved():
    # Must stay 12 (passlib's bcrypt default) so existing hashes aren't "weaker"
    # and new ones match — and so the embedded cost in stored hashes lines up.
    h = hash_password("CostFactorCheck1!")
    assert h.split("$")[2] == str(BCRYPT_ROUNDS) == "12"


def test_legacy_bcrypt_hash_still_verifies():
    # A standard "$2b$12$..." hash — byte-for-byte the format passlib produced —
    # of the password "LegacyBcryptPass!23". Direct bcrypt.checkpw must verify it
    # so users created before the migration can still log in.
    legacy = "$2b$12$b0gBL/1En6iSIWyKriQrxuyegXcyZyxzWOwFLtYf4bWpzXJWprYsG"
    assert verify_password("LegacyBcryptPass!23", legacy) is True
    assert verify_password("not-the-password", legacy) is False


def test_long_multibyte_password_does_not_raise():
    # 80 multibyte chars = 160 bytes. bcrypt 5 raises on >72 bytes; we truncate to
    # 72 bytes first, so hashing/verifying a long unicode password must not crash.
    pw = "é" * 80
    h = hash_password(pw)
    assert verify_password(pw, h) is True


def test_byte_truncation_matches_bcrypt_72_byte_limit():
    # Two passwords identical in their first 72 bytes verify interchangeably —
    # exactly bcrypt's own truncation behavior, which is what we replicate.
    base = "x" * 72
    h = hash_password(base)
    assert verify_password(base + "EXTRA-IGNORED-BEYOND-72", h) is True


def test_malformed_stored_hash_returns_false():
    assert verify_password("anything", "not-a-bcrypt-hash") is False
    assert verify_password("anything", "") is False


def test_unique_salts_per_hash():
    assert hash_password("samePassword!1") != hash_password("samePassword!1")


def test_passlib_context_removed():
    # The migration's whole point: the app stopped using passlib (its 1.7.4 pin
    # blocked bcrypt>=5). Assert the module no longer exposes the old
    # CryptContext — NOT that passlib is absent from site-packages, which is
    # environment-dependent (a cached CI env may still have it installed even
    # though it's dropped from requirements.txt) (Codex).
    import src.api.auth as auth_mod

    assert not hasattr(auth_mod, "pwd_context")
