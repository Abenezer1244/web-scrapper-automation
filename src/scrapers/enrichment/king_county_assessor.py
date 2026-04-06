"""King County property + mailing address enrichment via payment.kingcounty.gov.

Looks up parcel IDs directly on the Property Tax page to get:
- Property address (from Account Summary)
- Mailing address (from Account Summary → Mailing Address field)

URL: payment.kingcounty.gov/Home/Index?app=PropertyTaxes&Search={parcel_id}

No disclaimer, no login, no captcha. Just direct URL with parcel ID.
Cost: $0. Rate: ~3 seconds per parcel.
"""

import asyncio

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.enrichment.king_assessor")

_TAX_URL = "https://payment.kingcounty.gov/Home/Index?app=PropertyTaxes&Search="

add_scrape_domain("payment.kingcounty.gov")


async def batch_enrich_king_county(
    parcel_ids: list[str],
) -> dict[str, dict[str, str | None]]:
    """Look up multiple King County parcels via payment.kingcounty.gov.

    One browser session, one page load per parcel. No disclaimer needed.

    Returns:
        Dict mapping parcel_id → {property_address, mailing_address}.
    """
    results: dict[str, dict[str, str | None]] = {}

    if not parcel_ids:
        return results

    _logger.info("King County enrichment: %d parcels via payment.kingcounty.gov", len(parcel_ids))

    async with BridgeScraper() as scraper:
        for i, pid in enumerate(parcel_ids, 1):
            pid = pid.strip()
            if not pid or len(pid) < 6:
                continue

            if i % 50 == 0 or i == 1:
                _logger.info("Parcel %d/%d: %s", i, len(parcel_ids), pid)

            try:
                result = await _lookup_parcel(scraper, pid)
                if result.get("property_address") or result.get("mailing_address"):
                    results[pid] = result
            except Exception as exc:
                _logger.warning("Parcel %s error: %s", pid, str(exc)[:60])

            await asyncio.sleep(1)  # polite delay

    _logger.info("Enrichment done: %d/%d found", len(results), len(parcel_ids))
    return results


async def _lookup_parcel(scraper: BridgeScraper, parcel_id: str) -> dict[str, str | None]:
    """Load payment.kingcounty.gov tax page and extract both addresses in one shot."""
    empty = {"property_address": None, "mailing_address": None}

    await scraper.page.goto(
        f"{_TAX_URL}{parcel_id}",
        wait_until="domcontentloaded",
        timeout=20_000,
    )
    await scraper.page.wait_for_timeout(3000)

    # Scroll down to Account Summary
    await scraper.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await scraper.page.wait_for_timeout(1000)

    # Extract both addresses from the Account Summary section
    data = await scraper.page.evaluate("""
        (() => {
            const body = document.body.innerText || '';
            const result = {property: null, mailing: null};

            // Find Parcel Number line — property address is on the tax page title area
            // Look for the address pattern in "Account Summary" section
            const lines = body.split('\\n').map(l => l.trim()).filter(l => l.length > 0);

            // Find Mailing Address
            const mailIdx = body.indexOf('Mailing Address');
            if (mailIdx !== -1) {
                const afterMail = body.substring(mailIdx + 15).trim();
                const mailLines = afterMail.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
                const addrParts = [];
                for (const line of mailLines) {
                    if (/^(Pay by|Tax Account|Annual|This account|Parcel)/i.test(line)) break;
                    // Skip the "Mailing Address" label itself and "Pay by mail" link
                    const clean = line.replace(/Pay by mail/i, '').trim();
                    if (clean.length > 3 && clean.length < 100) {
                        addrParts.push(clean);
                    }
                    if (addrParts.length >= 3) break;
                }
                if (addrParts.length > 0) {
                    result.mailing = addrParts.join(', ');
                }
            }

            // Find property address — look for "Parcel Number" then the address nearby
            // Or extract from the taxpayer section
            for (let i = 0; i < lines.length; i++) {
                // Property address often appears near "Parcel Number" in Account Summary
                if (/^Parcel Number/i.test(lines[i])) {
                    // The property address might be 2-3 lines before or after
                    // Look for a line that looks like a street address
                    for (let j = Math.max(0, i-3); j < Math.min(lines.length, i+5); j++) {
                        const line = lines[j];
                        if (/^\\d+\\s+[A-Z]/.test(line) && /\\b(ST|AVE|DR|LN|WAY|BLVD|CT|PL|RD|CIR)\\b/i.test(line)) {
                            result.property = line.trim();
                            break;
                        }
                    }
                }
            }

            // If no property address found from Parcel Number, try the mailing as fallback
            if (!result.property && result.mailing) {
                // Extract just the street address (first line of mailing, skip name)
                const parts = result.mailing.split(',').map(p => p.trim());
                for (const part of parts) {
                    if (/^\\d+\\s+[A-Z]/.test(part) && /\\b(ST|AVE|DR|LN|WAY|BLVD|CT|PL|RD|CIR)\\b/i.test(part)) {
                        result.property = part;
                        break;
                    }
                }
            }

            return result;
        })()
    """)

    prop = data.get("property")
    mail = data.get("mailing")

    if prop:
        prop = " ".join(prop.strip().split())
    if mail:
        mail = " ".join(mail.strip().split())

    return {
        "property_address": prop,
        "mailing_address": mail,
    }
