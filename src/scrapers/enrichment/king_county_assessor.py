"""King County property + mailing address enrichment via payment.kingcounty.gov.

Looks up parcel IDs directly on the Property Tax page to get:
- Property address (from Account Summary)
- Mailing address (from Account Summary → Mailing Address field)

URL: payment.kingcounty.gov/Home/Index?app=PropertyTaxes&Search={parcel_id}

No disclaimer, no login, no captcha. Just direct URL with parcel ID.
Uses 5 parallel browser tabs for ~5x speedup.
Cost: $0. Rate: ~1 second per parcel (5 in parallel).
"""

import asyncio

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.enrichment.king_assessor")

_TAX_URL = "https://payment.kingcounty.gov/Home/Index?app=PropertyTaxes&Search="
_PARALLEL_TABS = 5

add_scrape_domain("payment.kingcounty.gov")

# JS extraction script (shared across all tabs)
_EXTRACT_JS = """
    (() => {
        const body = document.body.innerText || '';
        const result = {property: null, mailing: null};

        const lines = body.split('\\n').map(l => l.trim()).filter(l => l.length > 0);

        // Find Mailing Address
        const mailIdx = body.indexOf('Mailing Address');
        if (mailIdx !== -1) {
            const afterMail = body.substring(mailIdx + 15).trim();
            const mailLines = afterMail.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
            const addrParts = [];
            for (const line of mailLines) {
                if (/^(Pay by|Tax Account|Annual|This account|Parcel)/i.test(line)) break;
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

        // Find property address near "Parcel Number"
        for (let i = 0; i < lines.length; i++) {
            if (/^Parcel Number/i.test(lines[i])) {
                for (let j = Math.max(0, i-3); j < Math.min(lines.length, i+5); j++) {
                    const line = lines[j];
                    if (/^\\d+\\s+[A-Z]/.test(line) && /\\b(ST|AVE|DR|LN|WAY|BLVD|CT|PL|RD|CIR)\\b/i.test(line)) {
                        result.property = line.trim();
                        break;
                    }
                }
            }
        }

        // Fallback: extract street from mailing
        if (!result.property && result.mailing) {
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
"""


async def batch_enrich_king_county(
    parcel_ids: list[str],
) -> dict[str, dict[str, str | None]]:
    """Look up multiple King County parcels using parallel browser tabs.

    Opens 5 tabs, processes parcels in batches of 5 simultaneously.
    ~5x faster than sequential: 667 parcels in ~9 min instead of 45 min.

    Returns:
        Dict mapping parcel_id → {property_address, mailing_address}.
    """
    results: dict[str, dict[str, str | None]] = {}

    # Dedupe and clean
    clean_ids = []
    for pid in parcel_ids:
        pid = pid.strip()
        if pid and len(pid) >= 6 and pid not in [p for p in clean_ids]:
            clean_ids.append(pid)

    if not clean_ids:
        return results

    _logger.info("King County enrichment: %d parcels, %d parallel tabs", len(clean_ids), _PARALLEL_TABS)

    async with BridgeScraper() as scraper:
        # Create extra tabs (scraper already has 1 page)
        context = scraper._context
        pages = [scraper.page]
        for _ in range(_PARALLEL_TABS - 1):
            pages.append(await context.new_page())

        # Process in batches of _PARALLEL_TABS
        total = len(clean_ids)
        for batch_start in range(0, total, _PARALLEL_TABS):
            batch = clean_ids[batch_start:batch_start + _PARALLEL_TABS]

            if batch_start % 50 == 0 or batch_start == 0:
                _logger.info("Enriching %d-%d / %d...", batch_start + 1, min(batch_start + len(batch), total), total)

            # Launch all tabs in parallel
            tasks = []
            for i, pid in enumerate(batch):
                page = pages[i]
                tasks.append(_lookup_on_page(page, pid))

            # Wait for all tabs to finish
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            # Collect results
            for pid, result in zip(batch, batch_results):
                if isinstance(result, Exception):
                    continue
                if result and (result.get("property_address") or result.get("mailing_address")):
                    results[pid] = result

            await asyncio.sleep(0.5)  # brief pause between batches

        # Close extra pages
        for page in pages[1:]:
            await page.close()

    _logger.info("Enrichment done: %d/%d found", len(results), len(clean_ids))
    return results


async def _lookup_on_page(page, parcel_id: str) -> dict[str, str | None]:
    """Load tax page on a specific tab and extract addresses."""
    try:
        await page.goto(
            f"{_TAX_URL}{parcel_id}",
            wait_until="domcontentloaded",
            timeout=15_000,
        )
        await page.wait_for_timeout(2500)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(500)

        data = await page.evaluate(_EXTRACT_JS)

        prop = data.get("property")
        mail = data.get("mailing")
        if prop:
            prop = " ".join(prop.strip().split())
        if mail:
            mail = " ".join(mail.strip().split())

        return {"property_address": prop, "mailing_address": mail}

    except Exception:
        return {"property_address": None, "mailing_address": None}
