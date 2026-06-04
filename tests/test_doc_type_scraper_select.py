"""Phase 2b: King/Pierce scrapers honor an explicit doc-type selection.

Constructing a scraper does NOT launch a browser or touch a DB (that happens in
__aenter__), so these are effectively pure unit tests of the constructor logic.
`doc_types=None` must preserve legacy behavior (no shrink).
"""
from src.scrapers.king_wa_probate import KingCountyLandmarkWebScraper
from src.scrapers.pierce_wa_probate import PierceWAARMSScraper


def test_pierce_none_is_legacy_all_four_ids():
    s = PierceWAARMSScraper(record_type="pre_foreclosure")
    assert s.DOC_TYPE_IDS == ["187", "188", "146", "324"]


def test_pierce_selection_narrows_to_chosen_checkbox_ids():
    s = PierceWAARMSScraper(record_type="pre_foreclosure", doc_types=["notice_of_default", "lis_pendens"])
    assert set(s.DOC_TYPE_IDS) == {"187", "146"}


def test_pierce_doc_types_ignored_for_non_pre_foreclosure():
    s = PierceWAARMSScraper(record_type="probate", doc_types=["notice_of_default"])
    assert s.DOC_TYPE_IDS == ["226"]


def test_pierce_empty_selection_falls_back_to_legacy():
    # An unmappable/empty result must not scrape nothing — fall back to legacy.
    s = PierceWAARMSScraper(record_type="pre_foreclosure", doc_types=["foreclosure"])  # not a Pierce token
    assert s.DOC_TYPE_IDS == ["187", "188", "146", "324"]


def test_king_none_is_legacy_nots():
    s = KingCountyLandmarkWebScraper(record_type="pre_foreclosure")
    assert s.DOC_TYPE_SEARCH_TEXTS == ["notice of trustee sale"]


def test_king_selection_maps_to_search_text():
    s = KingCountyLandmarkWebScraper(record_type="pre_foreclosure", doc_types=["notice_of_trustee_sale"])
    assert s.DOC_TYPE_SEARCH_TEXTS == ["notice of trustee sale"]
