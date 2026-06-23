"""Tests for the CollectionScope SHOW descriptor shape and the base hook default.

These are pure-data tests (no DB, no network). They cover Phase A1: the shared
shape and the safe base default. Per-connector descriptors are covered as they
are implemented (Phase A2+).
"""
from src.scrapers.doc_scope import (
    CollectionScope,
    DocTypeItem,
    dataset,
    document_types,
)


def test_doc_type_item_defaults_to_approximate():
    item = DocTypeItem(label="Notice of Trustee Sale")
    assert item.exact is False
    assert item.label == "Notice of Trustee Sale"


def test_document_types_builder_dedups_case_insensitive_and_upgrades_exact():
    scope = document_types(
        [
            ("Notice of Trustee Sale", False),
            ("NOTICE OF TRUSTEE SALE", True),  # same label, confirms exact
            ("Lis Pendens", True),
        ]
    )
    assert scope.kind == "document_type"
    labels = [i.label for i in scope.items]
    # de-duplicated by case-insensitive label, first-seen order preserved
    assert labels == ["Notice of Trustee Sale", "Lis Pendens"]
    by_label = {i.label: i for i in scope.items}
    # approximate label was upgraded to exact when a later source confirmed it
    assert by_label["Notice of Trustee Sale"].exact is True
    assert by_label["Lis Pendens"].exact is True


def test_document_types_skips_blank_labels():
    scope = document_types([("", True), ("  ", False), ("Will", True)])
    assert [i.label for i in scope.items] == ["Will"]


def test_dataset_scope_has_no_items_and_keeps_note():
    note = "Collected from the county code-enforcement dataset; doc-type filtering not used."
    scope = dataset(note)
    assert scope.kind == "dataset"
    assert scope.items == ()
    assert scope.note == note


def test_to_api_shape():
    scope = CollectionScope(
        kind="document_type",
        items=(DocTypeItem("Death Certificate", exact=True),),
        note=None,
    )
    assert scope.to_api() == {
        "kind": "document_type",
        "items": [{"label": "Death Certificate", "exact": True}],
        "note": None,
    }


def test_base_scraper_default_scope_is_none():
    # Imported lazily so the pure-data tests above don't pull the Playwright stack.
    from src.scrapers.base_scraper import BridgeScraper

    assert BridgeScraper.collection_scope("probate") is None
    assert BridgeScraper.collection_scope("pre_foreclosure") is None
