"""Phase 2b: canonical doc-type registry — pure, no DB/scraper."""
from src.scrapers.doc_types import (
    CANONICAL_DOC_TYPES,
    availability_for,
    canonical_tokens_for,
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
