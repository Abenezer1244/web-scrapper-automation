"""Benton's cover-sheet parcel fill: what happens to a row whose document cannot be used.

The read itself (a real recorded image's cover sheet) is covered by
tests/test_recording_cover_sheet.py; these cover the paths decided before any document
is fetched, and how each outcome gates the owner-name fallback.
"""
from types import SimpleNamespace

import pytest

from src.scrapers.base_scraper import ScrapedRecord
from src.scrapers.reliability import ScraperExecutionError, TransientScrapeError
from src.scrapers.templates import eagleweb
from src.scrapers.templates.eagleweb import EagleWebScraper
from src.workers.tasks_helpers.enrich import name_lookup_eligible

_BENTON = "https://erecording.co.benton.wa.us/recorder/web/"


def _scraper(record_type="death_certificate"):
    return EagleWebScraper(_BENTON, "benton", "wa", [record_type], record_type=record_type)


def _row(href=None, parcel=None):
    r = ScrapedRecord()
    r.party_name, r.parcel_id = "DOE, JANE", parcel
    r._document_href = href
    return r


async def test_a_row_without_a_document_link_is_saved_unreadable():
    """No link is the document, not the transport: re-running would meet it again (Codex P2)."""
    row = _row()
    await _scraper()._fill_parcels_from_cover_sheets([row])
    assert row.parcel_id is None
    assert row.enrichment_data["recording_apn_lookup"] == "unreadable"


async def test_a_link_to_another_host_is_never_fetched():
    href = "https://evil.example/recorder/eagleweb/viewAttachment.jsp?docName=2026_028641&id=DOC1.A0&parent=DOC1"
    row = _row(href)
    await _scraper()._fill_parcels_from_cover_sheets([row])
    assert row.enrichment_data["recording_apn_lookup"] == "unreadable"


async def test_too_many_rows_is_a_permanent_error_not_a_retry(monkeypatch):
    """Retrying the same window cannot shrink it (Codex P2)."""
    monkeypatch.setattr(eagleweb, "_RECORDED_DOCUMENT_CAP", 1)
    with pytest.raises(ScraperExecutionError, match="run a shorter date range") as err:
        await _scraper()._fill_parcels_from_cover_sheets([_row(), _row()])
    assert not isinstance(err.value, TransientScrapeError)


async def test_rows_that_already_have_a_parcel_are_not_read():
    row = _row(parcel="101892030007001")
    await _scraper()._fill_parcels_from_cover_sheets([row])
    assert "recording_apn_lookup" not in row.enrichment_data


def test_only_probate_and_death_certificates_use_the_cover_sheet():
    assert eagleweb._COVER_SHEET_PARCEL_TYPES == {"probate", "death_certificate"}


@pytest.mark.parametrize("outcome, eligible", [
    ("none", True), ("found", False), ("unreadable", False), ("ambiguous", False)])
def test_only_a_recording_naming_no_parcel_may_be_looked_up_by_name(outcome, eligible):
    """Codex P1: an unreadable or ambiguous cover sheet must not get some other
    property attached through the owner-name search."""
    res = SimpleNamespace(party_name="DOE, JANE", property_address=None,
                          enrichment_data={"recording_apn_lookup": outcome})
    assert name_lookup_eligible(res) is eligible


def test_attachment_links_benton_names_otherwise_are_read():
    """2026-021430's attachment is named "Deed" (live 2026-11-17)."""
    from src.scrapers.templates.eagleweb import _recorded_document_url

    host = "erecording.co.benton.wa.us"
    base = f"https://{host}/recorder/eagleweb/viewAttachment.jsp?"
    assert _recorded_document_url(host, base + "docName=Deed&id=DOC1824S5607.A0&parent=DOC1824S5607") == (
        f"https://{host}/recorder/eagleweb/downloads/Deed.pdf?id=DOC1824S5607.A0&parent=DOC1824S5607")
    # Only the first attachment is the recording (".A1" may be a supplemental PDF).
    assert _recorded_document_url(host, base + "docName=2026_024579&id=DOC1823S2554.A1&parent=DOC1823S2554") is None
    # Another host, or characters that could change the URL, are never fetched.
    assert _recorded_document_url(host, base.replace(host, "evil.example") + "docName=Deed&id=DOC1.A0&parent=DOC1") is None
    assert _recorded_document_url(host, base + "docName=../x&id=DOC1.A0&parent=DOC1") is None
    assert _recorded_document_url(host, base + "docName=Deed&id=DOC1.A0%26x%3D1&parent=DOC1") is None
    assert _recorded_document_url(host, None) is None


def test_a_malformed_attachment_link_is_rejected_not_raised():
    from src.scrapers.templates.eagleweb import _recorded_document_url

    assert _recorded_document_url("erecording.co.benton.wa.us", "https://[bad/recorder?docName=Deed&id=DOC1.A0&parent=DOC1") is None
