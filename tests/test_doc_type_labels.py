"""Tests for the pre-foreclosure doc-type selector payload (UI contract).

The frontend renders `pre_foreclosure_doc_types` as a {token: label} map of
checkboxes. Regression guard: the /connectors endpoint must send that shape, NOT
the richer selectable_availability() metadata object (whose keys would render as
bogus "available"/"default"/... checkboxes).
"""
from src.scrapers.doc_types import (
    CANONICAL_DOC_TYPE_LABELS,
    availability_for,
    selectable_doc_type_labels,
)


def test_king_labels_are_token_to_label_map():
    labels = selectable_doc_type_labels("king", "wa")
    assert labels == {"notice_of_trustee_sale": "Notice of Trustee Sale"}


def test_pierce_labels_are_token_to_label_map():
    labels = selectable_doc_type_labels("pierce", "wa")
    assert labels == {
        "notice_of_default": "Notice of Default",
        "notice_of_trustee_sale": "Notice of Trustee Sale",
        "lis_pendens": "Lis Pendens",
        "notice_of_foreclosure": "Notice of Foreclosure",
    }


def test_unsupported_county_returns_none():
    # lewis (EagleWeb, health=down) is deferred → not in _EAGLEWEB_COUNTIES → hidden.
    assert selectable_doc_type_labels("lewis", "wa") is None
    assert selectable_doc_type_labels("nowhere", "zz") is None


def test_kitsap_eagleweb_labels_are_token_to_label_map():
    # Phase B: kitsap (EagleWeb) is now selectable (confidence=keyword) — 4 canonical
    # types, no notice_of_foreclosure (EagleWeb keyword map has no such token).
    labels = selectable_doc_type_labels("kitsap", "wa")
    assert labels == {
        "notice_of_default": "Notice of Default",
        "notice_of_trustee_sale": "Notice of Trustee Sale",
        "lis_pendens": "Lis Pendens",
        "foreclosure": "Foreclosure",
    }


def test_labels_shape_is_flat_str_to_str():
    """Guards against regressing back to the metadata object the UI can't render."""
    for county in ("king", "pierce"):
        labels = selectable_doc_type_labels(county, "wa")
        assert isinstance(labels, dict)
        for token, label in labels.items():
            assert isinstance(token, str) and isinstance(label, str)
        # Must NOT contain metadata keys.
        assert "available" not in labels
        assert "confidence" not in labels


def test_every_selectable_token_has_a_label_and_matches_availability():
    for county in ("king", "pierce"):
        avail = availability_for(county, "wa")["available"]
        labels = selectable_doc_type_labels(county, "wa")
        assert set(labels.keys()) == set(avail)
        for token in avail:
            assert token in CANONICAL_DOC_TYPE_LABELS
