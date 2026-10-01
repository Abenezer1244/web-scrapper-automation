"""AcclaimWeb's owner-name PACS address lookup only targets counties that run Tyler PACS.

Douglas County WA never resolved at `pacs.co.douglas.wa.us` (found by the D5-03 egress
probe, 2026-09-30): its assessor publishes TaxSifter (Aumentum Public Access,
douglaswa-taxsifter.publicaccessnow.com), which the PACS client cannot talk to. Every
Douglas run logged a misleading "PACS URL failed SSRF validation" warning (the DNS
lookup failed) before skipping. Douglas now uses its TaxSifter portal
(tests/test_taxsifter.py); a county with no portal at all skips before any network call.
"""
import logging

from src.scrapers.templates.acclaimweb import AcclaimWebScraper


async def test_a_county_with_no_assessor_portal_skips_without_a_network_call(caplog):
    """REGRESSION (#414): Douglas resolved a dead PACS host and logged an SSRF warning.
    A county with neither a PACS URL nor a TaxSifter origin now skips first thing."""
    scraper = AcclaimWebScraper("https://edocs.example.gov/AcclaimWeb", "nowhere", "WA")
    with caplog.at_level(logging.INFO, logger="scraper.template.acclaimweb"):
        await scraper._lookup_pacs_addresses([])
    messages = [r.getMessage() for r in caplog.records if r.name == "scraper.template.acclaimweb"]
    assert "No PACS URL for nowhere — skipping address lookup" in messages
    # The early return precedes the "Looking up addresses ..." line, the SSRF validation
    # (the DNS lookup) and every request, so none of them ran.
    assert not [m for m in messages if m.startswith("Looking up addresses") or "SSRF" in m]


def test_douglas_uses_taxsifter_not_pacs():
    from src.scrapers.enrichment.taxsifter import TAXSIFTER_ORIGINS

    assert "douglas" not in AcclaimWebScraper._PACS_URLS
    assert "douglas" in TAXSIFTER_ORIGINS


def test_chelan_keeps_its_pacs_url():
    """CONTROL: Chelan runs Tyler PACS PropertyAccess."""
    assert AcclaimWebScraper._PACS_URLS["chelan"].startswith("https://pacs.co.chelan.wa.us/")
