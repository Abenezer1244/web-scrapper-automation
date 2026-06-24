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
from src.scrapers.templates.skagit_recording import SkagitRecordingScraper


def _skagit(**kw):
    return SkagitRecordingScraper(
        base_url="https://www.skagitcounty.net/Search/Recording/",
        county="skagit", state="WA", record_types=["pre_foreclosure"], **kw
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


def test_king_none_is_legacy_nots():
    s = KingCountyLandmarkWebScraper(record_type="pre_foreclosure")
    assert s.DOC_TYPE_SEARCH_TEXTS == ["notice of trustee sale"]


def test_king_selection_maps_to_search_text():
    s = KingCountyLandmarkWebScraper(record_type="pre_foreclosure", doc_types=["notice_of_trustee_sale"])
    assert s.DOC_TYPE_SEARCH_TEXTS == ["notice of trustee sale"]
