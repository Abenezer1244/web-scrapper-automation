"""Phase 2b: canonical doc-type registry — pure, no DB/scraper."""
import pytest

from src.scrapers.doc_types import (
    CANONICAL_DOC_TYPES,
    availability_for,
    canonical_tokens_for,
    canonical_tokens_or_raise,
    normalize_doc_type,
    validate_selection,
)


def test_canonical_vocab():
    assert set(CANONICAL_DOC_TYPES) == {
        "notice_of_default", "notice_of_trustee_sale",
        "lis_pendens", "notice_of_foreclosure", "foreclosure",
    }


def test_normalize_maps_raw_strings():
    assert normalize_doc_type("NOTICE OF TRUSTEE SALE") == "notice_of_trustee_sale"
    assert normalize_doc_type("Trustee's Sale") == "notice_of_trustee_sale"
    assert normalize_doc_type("NOTICE OF DEFAULT") == "notice_of_default"
    assert normalize_doc_type("LIS PENDENS") == "lis_pendens"
    assert normalize_doc_type("totally unknown doc") is None


def test_availability_king_is_nots_only():
    a = availability_for("king", "wa")
    assert a is not None
    assert a["available"] == ["notice_of_trustee_sale"]
    assert a["default"] == "notice_of_trustee_sale"
    assert a["confidence"] == "verified"


def test_availability_pierce_has_four_default_nod():
    a = availability_for("pierce", "wa")
    assert set(a["available"]) == {
        "notice_of_default", "notice_of_trustee_sale",
        "lis_pendens", "notice_of_foreclosure",
    }
    assert a["default"] == "notice_of_default"


def test_unknown_county_fails_closed():
    assert availability_for("nowhere", "zz") is None


def test_validate_selection_rejects_unavailable_for_county():
    ok, err = validate_selection("king", "wa", ["notice_of_default"])
    assert ok is False and "notice_of_default" in err
    ok, err = validate_selection("king", "wa", ["notice_of_trustee_sale"])
    assert ok is True and err is None
    ok, err = validate_selection("king", "wa", [])
    assert ok is False


def test_canonical_tokens_pierce_maps_to_checkbox_ids():
    toks = canonical_tokens_for("pierce", "wa", ["notice_of_default", "lis_pendens"])
    assert set(toks) == {"187", "146"}


def test_canonical_tokens_king_maps_to_search_text():
    toks = canonical_tokens_for("king", "wa", ["notice_of_trustee_sale"])
    assert toks == ["notice of trustee sale"]


def test_validate_rejects_non_selectable_pre_foreclosure_county():
    # snohomish is a pre_foreclosure county but single-type (NTS newspaper) — it has
    # no doc-type selector, so a selection must be rejected (fail-closed).
    ok, err = validate_selection("snohomish", "wa", ["notice_of_default"])
    assert ok is False and err is not None


def test_validate_accepts_enabled_eagleweb_county():
    # Phase B: kitsap (EagleWeb) is now selectable.
    ok, err = validate_selection("kitsap", "wa", ["notice_of_default"])
    assert ok is True and err is None
    # but a type EagleWeb's keyword map doesn't cover is still rejected
    ok2, _ = validate_selection("kitsap", "wa", ["notice_of_foreclosure"])
    assert ok2 is False


def test_canonical_tokens_all_or_nothing_on_unmapped():
    # Any unmapped/stale type -> [] so the scraper falls back to legacy, never a
    # wrongly-partial-narrowed set (Codex P2).
    assert canonical_tokens_for("pierce", "wa", ["notice_of_default", "foreclosure"]) == []
    # fully-mapped selection still narrows correctly
    assert set(canonical_tokens_for("pierce", "wa", ["notice_of_default", "lis_pendens"])) == {"187", "146"}


# ── Phase B: canonical_tokens_or_raise — FAIL-CLOSED for explicit selections ──
def test_or_raise_returns_subset_for_valid_selection():
    assert canonical_tokens_or_raise("king", "wa", ["notice_of_trustee_sale"]) == ["notice of trustee sale"]
    assert set(canonical_tokens_or_raise("pierce", "wa", ["notice_of_default", "lis_pendens"])) == {"187", "146"}


def test_or_raise_raises_on_unmappable_token():
    # Stale/unavailable type for an EXPLICIT selection must RAISE, never broaden to
    # the full set (Codex High: explicit selection must not silently broaden output).
    with pytest.raises(ValueError):
        canonical_tokens_or_raise("pierce", "wa", ["notice_of_default", "foreclosure"])


def test_or_raise_raises_on_hidden_or_unknown_county():
    with pytest.raises(ValueError):
        canonical_tokens_or_raise("snohomish", "wa", ["notice_of_default"])  # single-type, not selectable
    with pytest.raises(ValueError):
        canonical_tokens_or_raise("nowhere", "zz", ["notice_of_default"])


def test_or_raise_raises_on_empty_selection():
    # Empty list is a programming error here — None means legacy/full, not empty.
    with pytest.raises(ValueError):
        canonical_tokens_or_raise("king", "wa", [])
