"""AcclaimWeb's owner-name PACS address lookup only targets counties that run Tyler PACS.

Douglas County WA never resolved at `pacs.co.douglas.wa.us` (found by the D5-03 egress
probe, 2026-09-30): its assessor publishes TaxSifter (Aumentum Public Access,
douglaswa-taxsifter.publicaccessnow.com), which the PACS client cannot talk to. Every
Douglas run logged a misleading "PACS URL failed SSRF validation" warning (the DNS
lookup failed) before skipping. It now skips honestly, before any network call.
"""
import logging

from src.scrapers.templates.acclaimweb import AcclaimWebScraper


async def test_douglas_skips_the_pacs_lookup_without_a_network_call(caplog):
    """REGRESSION: main resolved the dead host and logged an SSRF-validation warning."""
    scraper = AcclaimWebScraper("https://edocs.douglascountywa.gov/AcclaimWeb", "douglas", "WA")
    with caplog.at_level(logging.INFO, logger="scraper.template.acclaimweb"):
        await scraper._lookup_pacs_addresses([])
    messages = [r.getMessage() for r in caplog.records if r.name == "scraper.template.acclaimweb"]
    assert "No PACS URL for douglas — skipping address lookup" in messages
    # The early return precedes the "Looking up addresses ..." line, the SSRF validation
    # (the DNS lookup) and every request, so none of them ran.
    assert not [m for m in messages if m.startswith("Looking up addresses") or "SSRF" in m]
    assert "douglas" not in AcclaimWebScraper._PACS_URLS


def test_chelan_keeps_its_pacs_url():
    """CONTROL: Chelan runs Tyler PACS PropertyAccess."""
    assert AcclaimWebScraper._PACS_URLS["chelan"].startswith("https://pacs.co.chelan.wa.us/")
