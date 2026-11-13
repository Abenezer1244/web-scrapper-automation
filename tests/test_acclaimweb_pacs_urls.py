"""Where an AcclaimWeb lead's address comes from.

Douglas County WA never resolved at `pacs.co.douglas.wa.us` (found by the D5-03 egress
probe, 2026-09-30): its assessor publishes TaxSifter (Aumentum Public Access), so the
scrape looks owners up there (tests/test_taxsifter.py). A county with no such source
skips before any network call.

Chelan has no in-scrape owner lookup any more. The one it had posted to the Treasurer's
PACS portal without the search mode, so the portal answered every search with a 302 and
no lead ever got an address (live, 2026-11-12). Chelan leads now carry the APN of their
own recording (tests/test_acclaimweb_chelan.py), and the owner-name fallback is the
paced, license-gated enrichment pass, pointed at the Assessor's portal.
"""
import logging

from src.scrapers.templates.acclaimweb import AcclaimWebScraper, ChelanAcclaimWebScraper


async def test_a_county_with_no_owner_lookup_source_skips_without_a_network_call(caplog):
    """REGRESSION (#414): Douglas resolved a dead PACS host and logged an SSRF warning."""
    scraper = AcclaimWebScraper("https://edocs.example.gov/AcclaimWeb", "nowhere", "WA")
    with caplog.at_level(logging.INFO, logger="scraper.template.acclaimweb"):
        await scraper._lookup_addresses_by_owner([])
    messages = [r.getMessage() for r in caplog.records if r.name == "scraper.template.acclaimweb"]
    assert "No owner-name address source for nowhere in the scrape" in messages


def test_douglas_uses_taxsifter():
    from src.scrapers.enrichment.taxsifter import TAXSIFTER_ORIGINS

    assert "douglas" in TAXSIFTER_ORIGINS


def test_chelan_owner_fallback_is_the_assessor_portal_behind_the_license_gate():
    from src.scrapers.enrichment.assessor_urls import KNOWN_ASSESSOR_URLS
    from src.scrapers.enrichment.pacs import is_pacs_url
    from src.scrapers.enrichment.pacs_parcel import PACS_SITES

    url = KNOWN_ASSESSOR_URLS["chelan_WA"]
    assert is_pacs_url(url) and "cid=91" in url  # the Assessor's portal, not the Treasurer's (90)
    assert PACS_SITES["chelan"].license_restricted  # so enrich.py gates it on the kill switch


def test_only_chelan_reads_recording_apns():
    assert ChelanAcclaimWebScraper.READS_DETAIL_APN
    assert not AcclaimWebScraper.READS_DETAIL_APN
