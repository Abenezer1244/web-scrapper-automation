"""Regression tests for the Pierce ARMS record-type-pinned subclasses.

These were previously bare aliases to PierceWAARMSScraper (default record_type=
"probate"), so PierceWAPreForeclosureScraper() silently scraped probate instead
of pre-foreclosure. These tests lock in the correct behavior without a browser
or network — they only assert on the ARMS checkbox config the constructor
resolves. Mirrors tests/test_king_scraper_aliases.py.
"""
import inspect

import pytest

from src.scrapers.pierce_wa_probate import (
    PierceWAARMSScraper,
    PierceWADivorceScraper,
    PierceWAPreForeclosureScraper,
    PierceWAProbateScraper,
)

# (subclass, expected default record_type, expected DOC_TYPE_LABEL)
_PINNED = [
    (PierceWAProbateScraper, "probate", "PROBATE"),
    (PierceWAPreForeclosureScraper, "pre_foreclosure", "PRE-FORECLOSURE"),
    (PierceWADivorceScraper, "divorce", "DIVORCE"),
]


@pytest.mark.parametrize("cls,expected_rt,_label", _PINNED)
def test_subclass_signature_exposes_record_type_and_doc_types(cls, expected_rt, _label):
    """_run_scraper gates on exact param names, so both must be visible."""
    params = inspect.signature(cls).parameters
    assert "record_type" in params, f"{cls.__name__} hides record_type"
    assert "doc_types" in params, f"{cls.__name__} hides doc_types"
    assert params["record_type"].default == expected_rt


@pytest.mark.parametrize("cls,expected_rt,label", _PINNED)
def test_default_construction_resolves_correct_label(cls, expected_rt, label):
    """The bug: no-arg construction must resolve the named record type."""
    scraper = cls()
    assert scraper.DOC_TYPE_LABEL == label


def test_preforeclosure_default_includes_all_four_doc_types():
    """No selection = legacy full set (NOD, Notice of Foreclosure, Lis Pendens, NTS)."""
    scraper = PierceWAPreForeclosureScraper()
    assert set(scraper.DOC_TYPE_IDS) == {"187", "188", "146", "324"}


def test_preforeclosure_doc_type_selection_narrows_to_nts_checkbox():
    """An explicit Notice of Trustee Sale selection maps to ARMS checkbox 324."""
    scraper = PierceWAPreForeclosureScraper(doc_types=["notice_of_trustee_sale"])
    assert scraper.DOC_TYPE_IDS == ["324"]


def test_preforeclosure_doc_type_selection_narrows_to_nod_checkbox():
    scraper = PierceWAPreForeclosureScraper(doc_types=["notice_of_default"])
    assert scraper.DOC_TYPE_IDS == ["187"]


def test_subclasses_keep_base_record_type_override():
    """The worker overrides the default; the override must still win."""
    scraper = PierceWAProbateScraper(record_type="pre_foreclosure")
    assert scraper.DOC_TYPE_LABEL == "PRE-FORECLOSURE"


def test_subclasses_are_real_subclasses_of_base():
    for cls, _rt, _l in _PINNED:
        assert issubclass(cls, PierceWAARMSScraper)
