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


# ─── Template keyword coverage (Codex: fail on any newly unmapped keyword) ────

def _all_template_doc_type_maps():
    from src.scrapers.templates import (
        acclaimweb,
        ava_fidlar,
        eagleweb,
        idocmarket,
        landmarkweb,
        laserfiche_weblink,
        tyler_selfservice,
    )

    return {
        "acclaimweb": acclaimweb._DOC_TYPE_MAP,
        "ava_fidlar": ava_fidlar._DOC_TYPE_MAP,
        "eagleweb": eagleweb._DOC_TYPE_MAP,
        "idocmarket": idocmarket._DOC_TYPE_MAP,
        "landmarkweb": landmarkweb._DOC_TYPE_MAP,
        "laserfiche_weblink": laserfiche_weblink._DOC_TYPE_MAP,
        "tyler_selfservice": tyler_selfservice._DOC_TYPE_MAP,
    }


def test_every_template_keyword_is_classified():
    """Every keyword (except divorce, which is classifier-derived) must resolve to
    a display label or the explicit county-specific-codes bucket. A new, unmapped
    keyword fails here instead of silently mislabeling the customer-facing display."""
    from src.scrapers.doc_scope import classify_keyword

    unmapped: list[str] = []
    for template, dmap in _all_template_doc_type_maps().items():
        for record_type, keywords in dmap.items():
            if record_type == "divorce":
                continue  # divorce scope is derived from the shared classifier
            for kw in keywords:
                if classify_keyword(record_type, kw) is None:
                    unmapped.append(f"{template}:{record_type}:{kw}")
    assert not unmapped, f"Unclassified doc-type keywords (add to doc_scope presentation map): {unmapped}"


def test_each_wired_template_returns_scope_for_its_record_types():
    from src.scrapers.doc_scope import CollectionScope

    for template, dmap in _all_template_doc_type_maps().items():
        cls = _template_class(template)
        for record_type in dmap:
            scope = cls.collection_scope(record_type)
            assert isinstance(scope, CollectionScope), f"{template}:{record_type} returned {scope!r}"
            assert scope.items, f"{template}:{record_type} produced no items"
            # SHOW items are descriptive signals, never exact for keyword templates.
            assert all(i.exact is False for i in scope.items)


def _template_class(name: str):
    from src.scrapers.templates import (
        acclaimweb,
        ava_fidlar,
        eagleweb,
        idocmarket,
        landmarkweb,
        laserfiche_weblink,
        tyler_selfservice,
    )

    return {
        "acclaimweb": acclaimweb.AcclaimWebScraper,
        "ava_fidlar": ava_fidlar.AvaFidlarScraper,
        "eagleweb": eagleweb.EagleWebScraper,
        "idocmarket": idocmarket.IDocMarketScraper,
        "landmarkweb": landmarkweb.LandmarkWebScraper,
        "laserfiche_weblink": laserfiche_weblink.LaserficheWebLinkScraper,
        "tyler_selfservice": tyler_selfservice.TylerSelfServiceScraper,
    }[name]
