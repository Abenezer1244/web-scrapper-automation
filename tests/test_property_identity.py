"""Pure unit tests for property identity (re-keyed 2026-06-12).

No DB. Two keys are pinned independently:
- legacy_strong_signature: FROZEN parcel|address hash — the billing dedup_hash
  strong branch + enrichment-reuse gate. Golden-valued so a scheme change can
  never slip through (it would re-bill every delivered lead as new).
- compute_property_key: the overlap identity — parcel-primary (the 2026-06-12
  bug fix: address-format drift between pipelines must not split identity) and
  county/state-scoped (same parcel NUMBER in two counties must NOT collide).
"""
import hashlib

from src.workers.property_identity import (
    compute_property_key,
    is_strong_identity,
    legacy_strong_signature,
    normalize_address,
    normalize_parcel,
)


def test_normalize_parcel_strips_separators_and_uppercases():
    assert normalize_parcel(" 1234-56-7890 ") == "1234567890"
    assert normalize_parcel(None) == ""


def test_normalize_parcel_keeps_leading_zeros():
    # Codex 2026-06-12: stripping zeros could merge DISTINCT parcels — worse
    # than a missed overlap. Never lstrip globally.
    assert normalize_parcel("00371700101700") == "00371700101700"


def test_snohomish_house_formats_are_dedup_equivalent():
    """Snohomish PINs are 14 digits, but each trustee typesets them differently in
    the legal notice: Quality Loan prints them bare, North Star hyphenates (and not
    even consistently — 6-3-3-2 for one property, 6-2-4-2 for another). Those
    strings are stored VERBATIM because they are what the source published; it is
    normalization's job to make them compare equal, so the same parcel arriving
    from two trustees (or from the county tax file) is never billed twice.

    The four values below are the real Test 5 parcels, copied from the Snohomish
    County Tribune legals PDF.
    """
    quality_loan = ["00876100600800", "01133800000900"]
    north_star = ["008337-000-009-00", "010347-00-0086-00"]

    # Hyphens carry no identity — only the digits do.
    assert normalize_parcel("008337-000-009-00") == "00833700000900"
    assert normalize_parcel("010347-00-0086-00") == "01034700008600"
    # ...and every one of them is still 14 digits with its leading zeros intact.
    for parcel in quality_loan + north_star:
        assert len(normalize_parcel(parcel)) == 14, parcel
        assert normalize_parcel(parcel).startswith("0"), parcel

    # The same parcel published in either house style is ONE property.
    assert normalize_parcel("008337-000-009-00") == normalize_parcel("00833700000900")
    # ...while genuinely different parcels stay distinct.
    assert len({normalize_parcel(p) for p in quality_loan + north_star}) == 4


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


# ─── legacy_strong_signature: the FROZEN billing/dedup key ────────────────────


def test_legacy_signature_none_for_weak_identity():
    assert legacy_strong_signature(None, None) is None
    assert legacy_strong_signature("12", None) is None


def test_legacy_signature_golden_value_frozen():
    """GOLDEN VALUE: delivered_records rows in prod hold hashes from exactly
    this formula. If this test ever fails, billing dedup just broke — every
    already-delivered lead would re-read as new. Do not 'fix' the assertion."""
    got = legacy_strong_signature("1234-56-7890", "123 Main St., #4")
    assert got == hashlib.sha256(b"1234567890|123 MAIN ST 4").hexdigest()


def test_legacy_signature_is_old_parcel_pipe_address_format():
    parcel, addr = "1234567890", "123 Main St"
    expected = hashlib.sha256(
        f"{normalize_parcel(parcel)}|{normalize_address(addr)}".encode()
    ).hexdigest()
    assert legacy_strong_signature(parcel, addr) == expected


# ─── compute_property_key: the overlap identity (2026-06-12 scheme) ───────────


def test_property_key_is_none_for_weak_identity():
    assert compute_property_key(None, None, "king", "WA") is None
    assert compute_property_key("12", None, "king", "WA") is None


def test_property_key_parcel_primary_ignores_address_format_drift():
    """THE BUG FIX: same parcel, different address strings (tax situs with
    city+ZIP4 vs GIS street-only) must produce the SAME key."""
    tax = compute_property_key(
        "00371700101700", "8021 188TH ST SW, EDMONDS WA 98026-6022", "snohomish", "WA"
    )
    gis = compute_property_key(
        "00371700101700", "8021 188TH ST SW", "snohomish", "WA"
    )
    assert tax == gis and tax is not None


def test_property_key_same_parcel_different_county_does_not_collide():
    """County scoping: parcel numbers repeat across counties — bare-parcel
    hashing would manufacture false overlap (and cross-tenant-looking leads)."""
    king = compute_property_key("1234567890", None, "king", "WA")
    pierce = compute_property_key("1234567890", None, "pierce", "WA")
    assert king != pierce


def test_property_key_county_state_casing_normalized():
    a = compute_property_key("1234567890", None, "King", "wa")
    b = compute_property_key("1234567890", None, "king", "WA")
    assert a == b


def test_property_key_address_branch_when_no_parcel():
    """No parcel -> address identifies, county-scoped (street-only addresses
    repeat across cities)."""
    a = compute_property_key(None, "8021 188TH ST SW", "snohomish", "WA")
    b = compute_property_key(None, "8021 188TH ST SW", "king", "WA")
    assert a is not None and b is not None and a != b


def test_property_key_parcel_and_address_branches_cannot_collide():
    """Branch prefixes: a parcel string equal to an address string must not
    produce the same key."""
    p = compute_property_key("12345678", None, "king", "WA")
    a = compute_property_key(None, "12345678 X", "king", "WA")
    assert p != a


def test_property_key_diverges_from_legacy_by_design():
    """The 2026-06-12 split: overlap key != billing key for the same inputs.
    (If these ever re-converge someone re-unified the functions — see the
    module docstring for why that re-bills every lead.)"""
    parcel, addr = "1234567890", "123 Main St"
    assert compute_property_key(parcel, addr, "king", "WA") != legacy_strong_signature(
        parcel, addr
    )


def test_property_key_stable_across_parcel_formatting():
    a = compute_property_key("1234-56-7890", "123 Main St.", "king", "WA")
    b = compute_property_key("1234567890", "123 MAIN ST", "king", "WA")
    assert a == b and a is not None
