"""Pure unit tests for the canonical property-identity helper (Phase 1).

No DB. These pin the strong-identity rules so they stay in lockstep with
_compute_dedup_hash's strong branch in src/workers/tasks.py.
"""
import hashlib

from src.workers.property_identity import (
    compute_property_key,
    is_strong_identity,
    normalize_address,
    normalize_parcel,
)


def test_normalize_parcel_strips_separators_and_uppercases():
    assert normalize_parcel(" 1234-56-7890 ") == "1234567890"
    assert normalize_parcel(None) == ""


def test_normalize_address_collapses_punctuation_and_whitespace():
    assert normalize_address("123 Main St., #4  ") == "123 MAIN ST 4"
    assert normalize_address(None) == ""


def test_strong_identity_true_for_valid_parcel():
    assert is_strong_identity("123456", None) is True


def test_strong_identity_true_for_valid_address():
    assert is_strong_identity(None, "123 MAIN STREET") is True


def test_strong_identity_false_for_short_or_empty():
    assert is_strong_identity(None, None) is False
    assert is_strong_identity("12", "x") is False
    assert is_strong_identity("ABCD", None) is False
    assert is_strong_identity(None, "12345678") is False


def test_property_key_is_none_for_weak_identity():
    assert compute_property_key(None, None) is None
    assert compute_property_key("12", None) is None


def test_property_key_matches_dedup_hash_strong_key_format():
    parcel, addr = "1234567890", "123 Main St"
    expected = hashlib.sha256(
        f"{normalize_parcel(parcel)}|{normalize_address(addr)}".encode()
    ).hexdigest()
    assert compute_property_key(parcel, addr) == expected


def test_property_key_stable_across_formatting():
    a = compute_property_key("1234-56-7890", "123 Main St.")
    b = compute_property_key("1234567890", "123 MAIN ST")
    assert a == b and a is not None


def test_strong_key_equals_legacy_inline_formula():
    """Lockstep guard: the helper key must equal the legacy inline strong key
    that _compute_dedup_hash produced, so the Task 2 refactor is behavior-
    preserving for strong rows."""
    import hashlib, re
    parcel_in, addr_in = "1234-56-7890", "123 Main St., #4"
    parcel = (parcel_in or "").strip().upper().replace("-", "").replace(" ", "")
    addr = (addr_in or "").strip().upper()
    addr = re.sub(r"[\.,#]", " ", addr)
    addr = re.sub(r"\s+", " ", addr).strip()
    legacy = hashlib.sha256(f"{parcel}|{addr}".encode()).hexdigest()
    from src.workers.property_identity import compute_property_key
    assert compute_property_key(parcel_in, addr_in) == legacy
