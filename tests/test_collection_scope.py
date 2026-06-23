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
    """Every keyword map that feeds from_keyword_map(), template + keyword-bespoke."""
    from src.scrapers import clark_wa, whatcom_wa
    from src.scrapers.templates import (
        acclaimweb,
        ava_fidlar,
        eagleweb,
        idocmarket,
        landmarkweb,
        laserfiche_weblink,
        skagit_recording,
        tyler_selfservice,
    )

    return {
        "acclaimweb": acclaimweb._DOC_TYPE_MAP,
        "ava_fidlar": ava_fidlar._DOC_TYPE_MAP,
        "eagleweb": eagleweb._DOC_TYPE_MAP,
        "idocmarket": idocmarket._DOC_TYPE_MAP,
        "landmarkweb": landmarkweb._DOC_TYPE_MAP,
        "laserfiche_weblink": laserfiche_weblink._DOC_TYPE_MAP,
        "skagit_recording": skagit_recording._DOC_TYPE_MAP,
        "tyler_selfservice": tyler_selfservice._DOC_TYPE_MAP,
        "whatcom_wa": whatcom_wa._DOC_TYPE_KEYWORDS,
        "clark_wa": clark_wa._DOC_TYPES,
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


_KEYWORD_TEMPLATE_NAMES = (
    "acclaimweb",
    "ava_fidlar",
    "eagleweb",
    "idocmarket",
    "landmarkweb",
    "laserfiche_weblink",
    "tyler_selfservice",
)


def test_each_wired_template_returns_scope_for_its_record_types():
    from src.scrapers.doc_scope import CollectionScope

    maps = _all_template_doc_type_maps()
    for template in _KEYWORD_TEMPLATE_NAMES:
        cls = _template_class(template)
        for record_type in maps[template]:
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


# ─── SHOW vs SELECT separation + connector wiring ────────────────────────────

def test_show_labels_are_never_select_canonical_tokens():
    """SHOW labels are human-readable; they must never be the SELECT canonical
    tokens (e.g. 'notice_of_trustee_sale'). Conflating the two would turn an
    unverified display string into an accidental control-contract value."""
    from src.scrapers.doc_scope import classify_keyword
    from src.scrapers.doc_types import CANONICAL_DOC_TYPES

    canonical = set(CANONICAL_DOC_TYPES)
    for record_type, dmap_keys in (
        ("probate", ["DEATH CERTIFICATE", "WILL", "PROBATE"]),
        ("pre_foreclosure", ["NOTICE OF TRUSTEE SALE", "LIS PENDENS", "NOTICE OF DEFAULT"]),
        ("tax_delinquent", ["TAX LIEN", "TAX"]),
    ):
        for kw in dmap_keys:
            label = classify_keyword(record_type, kw)
            assert label not in canonical, f"SHOW label {label!r} collides with a SELECT token"
            assert label is None or "_" not in label  # human labels use spaces, not snake_case


def test_all_allowlisted_scraper_classes_expose_collection_scope():
    """Every manual-mode scraper the registry can resolve must answer
    collection_scope() (inherited default is fine) so the API never AttributeErrors."""
    from src.scrapers.registry import _ALLOWED_SCRAPER_MODULES

    for module_path in _ALLOWED_SCRAPER_MODULES:
        if module_path.endswith("base_scraper"):
            continue
        import importlib

        mod = importlib.import_module(module_path)
        # Find BridgeScraper subclasses defined in the module.
        from src.scrapers.base_scraper import BridgeScraper

        for name in dir(mod):
            obj = getattr(mod, name)
            if isinstance(obj, type) and issubclass(obj, BridgeScraper) and obj is not BridgeScraper:
                assert callable(obj.collection_scope)
                # Must not raise for an arbitrary record type.
                assert obj.collection_scope("nonexistent_type") is None


def test_clark_scope_derives_from_checkbox_selection_not_client_filter():
    """Clark's SHOW scope must reflect what the portal actually selects (checkbox
    IDs), not the broader client-side allowlist. Clark tax selects only checkbox 97
    (Federal Tax Lien), so the scope must NOT advertise Certificate of Delinquency /
    Certificate of Sale (which the scraper never requests). Codex P2 regression."""
    from src.scrapers.clark_wa import ClarkWAScraper

    tax = ClarkWAScraper.collection_scope("tax_delinquent")
    assert [i.label for i in tax.items] == ["Federal Tax Lien"]
    assert all(i.exact for i in tax.items)
    # divorce: Clark recorder holds no divorce decrees
    assert ClarkWAScraper.collection_scope("divorce") is None


def test_connector_scraper_class_resolves_and_blocks():
    from src.scrapers.registry import connector_scraper_class

    class _Manual:
        scraper_mode = "manual"
        scraper_class = "src.scrapers.clark_wa.ClarkWAScraper"
        base_url = ""

    class _Blocked:
        scraper_mode = "manual"
        scraper_class = "os.system"
        base_url = ""

    assert connector_scraper_class(_Manual()).__name__ == "ClarkWAScraper"
    assert connector_scraper_class(_Blocked()) is None
