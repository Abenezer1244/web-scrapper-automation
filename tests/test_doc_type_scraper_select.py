"""Phase 2b/B: King/Pierce scrapers honor an explicit doc-type selection.

Constructing a scraper does NOT launch a browser or touch a DB (that happens in
__aenter__), so these are effectively pure unit tests of the constructor logic.
`doc_types=None` must preserve legacy behavior (no shrink). An EXPLICIT selection
that can't be fully mapped (empty or stale/unmappable) must FAIL CLOSED (raise),
never silently broaden to the full legacy set (Phase B / Codex High).
"""
import pytest

from src.scrapers.clark_wa import ClarkWAScraper
from src.scrapers.king_wa_probate import KingCountyLandmarkWebScraper
from src.scrapers.pierce_wa_probate import PierceWAARMSScraper
from src.scrapers.templates.eagleweb import EagleWebScraper
from src.scrapers.templates.skagit_recording import SkagitRecordingScraper


def _skagit(**kw):
    return SkagitRecordingScraper(
        base_url="https://www.skagitcounty.net/Search/Recording/",
        county="skagit", state="WA", record_types=["pre_foreclosure"], **kw
    )


def _eagleweb(county="thurston", **kw):
    return EagleWebScraper(
        base_url="https://eagleweb.co.thurston.wa.us/thurstonrecorder/web/",
        county=county, state="WA", record_types=["pre_foreclosure"], **kw
    )


def test_pierce_none_is_legacy_all_four_ids():
    s = PierceWAARMSScraper(record_type="pre_foreclosure")
    assert s.DOC_TYPE_IDS == ["187", "188", "146", "324"]


def test_pierce_selection_narrows_to_chosen_checkbox_ids():
    s = PierceWAARMSScraper(record_type="pre_foreclosure", doc_types=["notice_of_default", "lis_pendens"])
    assert set(s.DOC_TYPE_IDS) == {"187", "146"}


def test_pierce_doc_types_ignored_for_non_pre_foreclosure():
    s = PierceWAARMSScraper(record_type="probate", doc_types=["notice_of_default"])
    assert s.DOC_TYPE_IDS == ["226"]


def test_pierce_unmappable_selection_raises():
    # An explicit selection with a type Pierce doesn't expose must FAIL CLOSED —
    # never silently broaden back to the full legacy set (Codex High).
    with pytest.raises(ValueError):
        PierceWAARMSScraper(record_type="pre_foreclosure", doc_types=["foreclosure"])  # not a Pierce token


def test_pierce_empty_selection_raises():
    # [] is an explicit (degenerate) selection, not legacy — fail closed.
    with pytest.raises(ValueError):
        PierceWAARMSScraper(record_type="pre_foreclosure", doc_types=[])


def test_king_unmappable_selection_raises():
    with pytest.raises(ValueError):
        KingCountyLandmarkWebScraper(record_type="pre_foreclosure", doc_types=["notice_of_default"])  # King = NTS only


def test_king_empty_selection_raises():
    with pytest.raises(ValueError):
        KingCountyLandmarkWebScraper(record_type="pre_foreclosure", doc_types=[])


# ── Clark (server-side checkbox codes + client-side label allowlist) ──────────
def test_clark_none_is_legacy_six_codes():
    s = ClarkWAScraper(record_type="pre_foreclosure")
    assert s._checkbox_values == ["167", "129", "166", "157", "93", "257"]


def test_clark_selection_narrows_codes_and_labels():
    # notice_of_trustee_sale expands to BOTH 167 and 257 (NTS + TRUSTEES SALE);
    # both the server codes and the client label allowlist must narrow together.
    s = ClarkWAScraper(
        record_type="pre_foreclosure",
        doc_types=["notice_of_trustee_sale", "lis_pendens"],
    )
    assert set(s._checkbox_values) == {"167", "257", "129"}
    assert set(s._doc_types) == {"NOTICE OF TRUSTEE SALE", "TRUSTEES SALE", "LIS PENDENS"}


def test_clark_single_foreclosure_narrows():
    s = ClarkWAScraper(record_type="pre_foreclosure", doc_types=["foreclosure"])
    assert s._checkbox_values == ["93"]
    assert s._doc_types == ["FORECLOSURE"]


def test_clark_empty_selection_raises():
    with pytest.raises(ValueError):
        ClarkWAScraper(record_type="pre_foreclosure", doc_types=[])


def test_clark_doc_types_ignored_for_non_pre_foreclosure():
    s = ClarkWAScraper(record_type="probate", doc_types=["foreclosure"])
    assert s._checkbox_values == ["62", "316", "340", "278"]


# ── Skagit (two-stage: server dropdown + client refine — both must narrow) ────
def test_skagit_none_is_legacy_both_stages():
    s = _skagit(record_type="pre_foreclosure")
    assert s._server_label_override is None
    assert s._refine_keyword_override is None


def test_skagit_selection_narrows_both_stages():
    s = _skagit(record_type="pre_foreclosure", doc_types=["notice_of_default", "lis_pendens"])
    # Stage 1: exact dropdown labels
    assert s._server_label_override == ["Notice Of Default", "Lis Pendens"]
    # Stage 2: matching client-refine keywords
    assert s._refine_keyword_override == ["NOTICE OF DEFAULT", "LIS PENDENS"]


def test_skagit_trustee_sale_expands_refine_keywords():
    s = _skagit(record_type="pre_foreclosure", doc_types=["notice_of_trustee_sale"])
    assert s._server_label_override == ["Notice Of Trustees Sale"]
    assert s._refine_keyword_override == ["NOTICE OF TRUSTEE", "TRUSTEE SALE", "TRUSTEE'S SALE"]


def test_skagit_unmappable_selection_raises():
    # Skagit's dropdown has no generic "Foreclosure" option — fail closed.
    with pytest.raises(ValueError):
        _skagit(record_type="pre_foreclosure", doc_types=["foreclosure"])


def test_skagit_empty_selection_raises():
    with pytest.raises(ValueError):
        _skagit(record_type="pre_foreclosure", doc_types=[])


def test_skagit_doc_types_ignored_for_non_pre_foreclosure():
    s = _skagit(record_type="probate", doc_types=["lis_pendens"])
    assert s._server_label_override is None and s._refine_keyword_override is None


def test_skagit_filter_by_type_applies_refine_override():
    # Record-level: an explicit notice_of_default selection must DROP a Lis Pendens
    # row via the narrowed client-refine keyword gate (Codex — assert real filtering,
    # not just the private override array).
    from src.scrapers.base_scraper import ScrapedRecord
    s = _skagit(record_type="pre_foreclosure", doc_types=["notice_of_default"])
    recs = [
        ScrapedRecord(doc_type="Notice Of Default", party_name="JOHN DOE", parcel_id="P1"),
        ScrapedRecord(doc_type="Lis Pendens", party_name="JANE SMITH", parcel_id="P2"),
    ]
    kept_types = {r.doc_type for r in s._filter_by_type(recs)}
    assert "Lis Pendens" not in kept_types  # dropped by narrowed keyword gate
    assert "Notice Of Default" in kept_types  # matching person row survives


# ── EagleWeb (client-side keyword filter, shared across 8 counties) ───────────
def test_eagleweb_tokens_partition_scraper_map():
    # The registry tokens MUST be an exact partition of the scraper's keyword map,
    # so a narrowed selection is always a true subset of legacy output (never adds a
    # keyword legacy wouldn't match, never drops one it would). Lockstep guard.
    from src.scrapers.doc_types import _EAGLEWEB_TEMPLATE
    from src.scrapers.templates.eagleweb import _DOC_TYPE_MAP
    token_union = set()
    for kws in _EAGLEWEB_TEMPLATE["tokens"].values():
        token_union.update(kws)
    assert token_union == set(_DOC_TYPE_MAP["pre_foreclosure"]), (
        token_union ^ set(_DOC_TYPE_MAP["pre_foreclosure"])
    )


def test_eagleweb_none_is_legacy():
    assert _eagleweb(record_type="pre_foreclosure")._doc_type_keyword_override is None


def test_eagleweb_selection_narrows_keywords():
    s = _eagleweb(record_type="pre_foreclosure", doc_types=["notice_of_trustee_sale"])
    assert s._doc_type_keyword_override == [
        "NOTICE OF TRUSTEE SALE", "TRUSTEE SALE", "TRUSTEE'S SALE", "NTS", "NTSCL"
    ]
    s2 = _eagleweb(record_type="pre_foreclosure", doc_types=["lis_pendens"])
    assert s2._doc_type_keyword_override == ["LIS PENDENS", "LISP"]


def test_eagleweb_unmappable_selection_raises():
    # EagleWeb has no notice_of_foreclosure token — fail closed.
    with pytest.raises(ValueError):
        _eagleweb(record_type="pre_foreclosure", doc_types=["notice_of_foreclosure"])


def test_eagleweb_empty_selection_raises():
    with pytest.raises(ValueError):
        _eagleweb(record_type="pre_foreclosure", doc_types=[])


def test_eagleweb_doc_types_ignored_for_non_pre_foreclosure():
    assert _eagleweb(record_type="probate", doc_types=["lis_pendens"])._doc_type_keyword_override is None


# ── P4 keyword families: partition-invariant + narrowing + fail-closed ────────
def _kw_map(modpath, attr="_DOC_TYPE_MAP"):
    import importlib
    return getattr(importlib.import_module(modpath), attr)["pre_foreclosure"]


# (county, scraper module, keyword-attr) — registry tokens must EXACTLY partition
# the scraper's pre_foreclosure keyword set (no over-collect, no silent drop).
_PARTITION_CASES = [
    ("douglas", "src.scrapers.templates.acclaimweb", "_DOC_TYPE_MAP"),
    ("columbia", "src.scrapers.templates.idocmarket", "_DOC_TYPE_MAP"),
    ("cowlitz", "src.scrapers.templates.laserfiche_weblink", "_DOC_TYPE_MAP"),
    ("okanogan", "src.scrapers.templates.tyler_selfservice", "_DOC_TYPE_MAP"),
    ("whatcom", "src.scrapers.whatcom_wa", "_DOC_TYPE_KEYWORDS"),
]


@pytest.mark.parametrize("county,modpath,attr", _PARTITION_CASES)
def test_keyword_family_tokens_partition_scraper_map(county, modpath, attr):
    from src.scrapers.doc_types import availability_for
    a = availability_for(county, "wa")
    assert a and a.get("supported_for_selection")
    token_union = set()
    for kws in a["tokens"].values():
        token_union.update(kws)
    scraper_kw = set(_kw_map(modpath, attr))
    assert token_union == scraper_kw, token_union ^ scraper_kw


def test_acclaim_douglas_narrows_and_fails_closed():
    from src.scrapers.templates.acclaimweb import AcclaimWebScraper
    def mk(**kw):
        return AcclaimWebScraper(base_url="https://edocs.douglascountywa.gov/AcclaimWeb",
                                 county="douglas", state="WA", record_types=["pre_foreclosure"], **kw)
    assert mk(record_type="pre_foreclosure")._doc_type_keyword_override is None
    assert mk(record_type="pre_foreclosure", doc_types=["lis_pendens"])._doc_type_keyword_override == ["LIS PENDENS"]
    with pytest.raises(ValueError):
        mk(record_type="pre_foreclosure", doc_types=["notice_of_foreclosure"])  # not available for Acclaim
    with pytest.raises(ValueError):
        mk(record_type="pre_foreclosure", doc_types=[])


def test_tyler_okanogan_groups_forfeit_under_foreclosure():
    from src.scrapers.templates.tyler_selfservice import TylerSelfServiceScraper
    s = TylerSelfServiceScraper(base_url="https://okanogancountywa-web.tylerhost.net/Web",
                                county="okanogan", state="WA", record_types=["pre_foreclosure"],
                                record_type="pre_foreclosure", doc_types=["foreclosure"])
    assert s._doc_type_keyword_override == ["FORECLOSURE", "NOTICE OF INTENT TO FORFEIT"]


def test_whatcom_explicit_selection_uses_canonical_set():
    from src.scrapers.whatcom_wa import WhatcomWAScraper
    # whatcom is the only keyword family exposing notice_of_foreclosure; an explicit
    # selection matches by canonical token (set), NOT substring keywords.
    s = WhatcomWAScraper(record_type="pre_foreclosure", doc_types=["notice_of_foreclosure"])
    assert s._selected_canonical == {"notice_of_foreclosure"}
    with pytest.raises(ValueError):
        WhatcomWAScraper(record_type="pre_foreclosure", doc_types=[])
    # legacy (no selection): keyword set, no canonical override
    legacy = WhatcomWAScraper(record_type="pre_foreclosure")
    assert legacy._selected_canonical is None and "FORECLOSURE" in legacy._keywords


def test_whatcom_foreclosure_does_not_leak_notice_of_foreclosure():
    # Codex High: a `foreclosure`-only selection must NOT pull NOTICE OF FORECLOSURE
    # rows. Canonical normalization keeps them distinct (longest-match-first).
    from src.scrapers.doc_types import normalize_doc_type
    selected = {"foreclosure"}
    assert normalize_doc_type("NOTICE OF FORECLOSURE") == "notice_of_foreclosure"
    assert normalize_doc_type("NOTICE OF FORECLOSURE") not in selected  # dropped
    assert normalize_doc_type("FORECLOSURE") in selected  # kept


def test_king_none_is_legacy_nots():
    s = KingCountyLandmarkWebScraper(record_type="pre_foreclosure")
    assert s.DOC_TYPE_SEARCH_TEXTS == ["notice of trustee sale"]


def test_king_selection_maps_to_search_text():
    s = KingCountyLandmarkWebScraper(record_type="pre_foreclosure", doc_types=["notice_of_trustee_sale"])
    assert s.DOC_TYPE_SEARCH_TEXTS == ["notice of trustee sale"]
