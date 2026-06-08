"""Regression tests for the King LandmarkWeb record-type-pinned subclasses.

These were previously bare aliases to the base class (default record_type=
"probate"), so KingWaPreForeclosureScraper() silently scraped death
certificates instead of Notice of Trustee Sale. These tests lock in the
correct behavior without needing a browser, captcha, or network — they only
assert on the search config the constructor resolves.
"""
import inspect

import pytest

from src.scrapers.king_wa_probate import (
    KingCountyLandmarkWebScraper,
    KingWaDivorceScraper,
    KingWaPreForeclosureScraper,
    KingWaProbateScraper,
    LandmarkWebDeathCertScraper,
)

# (subclass, expected default record_type, a substring expected in the
#  resolved document-type search text)
_PINNED = [
    (KingWaProbateScraper, "probate", "death cert"),
    (LandmarkWebDeathCertScraper, "death_certificate", "death cert"),
    (KingWaPreForeclosureScraper, "pre_foreclosure", "notice of trustee sale"),
    (KingWaDivorceScraper, "divorce", "dissolution"),
]


@pytest.mark.parametrize("cls,expected_rt,_search", _PINNED)
def test_subclass_signature_exposes_record_type_and_doc_types(cls, expected_rt, _search):
    """_run_scraper gates on exact param names, so both must be visible."""
    params = inspect.signature(cls).parameters
    assert "record_type" in params, f"{cls.__name__} hides record_type from inspect.signature"
    assert "doc_types" in params, f"{cls.__name__} hides doc_types from inspect.signature"
    assert params["record_type"].default == expected_rt


@pytest.mark.parametrize("cls,expected_rt,search", _PINNED)
def test_default_construction_resolves_correct_doc_type(cls, expected_rt, search):
    """The bug: no-arg construction must resolve the named record type."""
    scraper = cls()
    joined = " ".join(scraper.DOC_TYPE_SEARCH_TEXTS).lower()
    assert search in joined, (
        f"{cls.__name__}() resolved search texts {scraper.DOC_TYPE_SEARCH_TEXTS!r}, "
        f"expected one containing {search!r}"
    )


def test_preforeclosure_doc_type_selection_narrows_to_nts():
    """An explicit Notice of Trustee Sale selection maps to King's token."""
    scraper = KingWaPreForeclosureScraper(doc_types=["notice_of_trustee_sale"])
    joined = " ".join(scraper.DOC_TYPE_SEARCH_TEXTS).lower()
    assert "notice of trustee sale" in joined


def test_subclasses_keep_base_record_type_override():
    """The worker overrides the default; the override must still win."""
    scraper = KingWaProbateScraper(record_type="pre_foreclosure")
    joined = " ".join(scraper.DOC_TYPE_SEARCH_TEXTS).lower()
    assert "notice of trustee sale" in joined


def test_subclasses_are_real_subclasses_of_base():
    for cls, _rt, _s in _PINNED:
        assert issubclass(cls, KingCountyLandmarkWebScraper)
