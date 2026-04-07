"""King County address enrichment via eRealProperty + Property Tax Bill.

Flow (matches user's screenshots):
1. eRealProperty (blue.kingcounty.com/Assessor/eRealProperty/Dashboard.aspx?ParcelNbr=PID)
   → Site Address (property address)
   → "Property Tax Bill" link (has correct tax account number)
2. Property Tax Bill (payment.kingcounty.gov)
   → Mailing Address

Key: the parcel number (10 digits) != tax account number (12 digits).
eRealProperty bridges this — its "Property Tax Bill" link has the right account number.
"""

import asyncio

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.enrichment.king_assessor")

_ERP_URL = "https://blue.kingcounty.com/Assessor/eRealProperty/Dashboard.aspx?ParcelNbr="
_PARALLEL = 3

add_scrape_domain("blue.kingcounty.com")
add_scrape_domain("payment.kingcounty.gov")


async def batch_enrich_king_county(
    parcel_ids: list[str],
) -> dict[str, dict[str, str | None]]:
    """Enrich parcels: eRealProperty for property address, then Tax Bill for mailing."""
    results: dict[str, dict[str, str | None]] = {}
    clean = list(dict.fromkeys(pid.strip() for pid in parcel_ids if pid and len(pid.strip()) >= 6))

    if not clean:
        return results

    _logger.info("King County enrichment: %d parcels, %d tabs", len(clean), _PARALLEL)

    async with BridgeScraper() as scraper:
        pages = [scraper.page]
        for _ in range(_PARALLEL - 1):
            pages.append(await scraper._context.new_page())

        for batch_start in range(0, len(clean), _PARALLEL):
            batch = clean[batch_start:batch_start + _PARALLEL]

            if batch_start % 50 == 0:
                _logger.info("  %d / %d ...", batch_start, len(clean))

            tasks = [_lookup(pages[i], pid) for i, pid in enumerate(batch)]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for pid, res in zip(batch, batch_results):
                if isinstance(res, dict) and (res.get("property_address") or res.get("mailing_address")):
                    results[pid] = res

            await asyncio.sleep(0.5)

        for p in pages[1:]:
            await p.close()

    _logger.info("Enrichment done: %d/%d found", len(results), len(clean))
    return results


async def _lookup(page, parcel_id: str) -> dict[str, str | None]:
    """Step 1: eRealProperty for property address + tax bill link.
       Step 2: Property Tax Bill for mailing address."""
    try:
        # Step 1: Go to eRealProperty Dashboard (no disclaimer needed for Dashboard URL)
        await page.goto(f"{_ERP_URL}{parcel_id}", wait_until="load", timeout=15_000)
        await page.wait_for_timeout(3000)

        # Extract Site Address from the PARCEL table
        prop = await page.evaluate("""
            (() => {
                // Look for "Site Address" in table cells
                const tds = document.querySelectorAll('td');
                for (let i = 0; i < tds.length; i++) {
                    if (tds[i].textContent.trim() === 'Site Address' && tds[i+1]) {
                        return tds[i+1].textContent.trim();
                    }
                }
                return null;
            })()
        """)

        # Get the Property Tax Bill link (has the correct tax account number)
        tax_url = await page.evaluate("""
            (() => {
                const link = document.querySelector('#cphContent_HyperLinkPropertyTaxInformationSystem, a[href*="PropertyTaxes"]');
                return link ? link.href : null;
            })()
        """)

        # Step 2: Follow the Tax Bill link for mailing address
        mailing = None
        if tax_url:
            await page.goto(tax_url, wait_until="load", timeout=15_000)
            try:
                await page.wait_for_function(
                    "() => document.body.innerText.includes('Mailing Address') || document.body.innerText.includes('No accounts')",
                    timeout=8_000,
                )
            except Exception:
                pass

            body = await page.inner_text("body")
            if "Mailing Address" in body:
                idx = body.index("Mailing Address") + len("Mailing Address")
                after = body[idx:idx + 200]
                lines = [l.strip() for l in after.split("\n") if l.strip()]
                addr_lines = []
                for line in lines:
                    if line.startswith("Pay by") or line.startswith("Annual") or line.startswith("Billing"):
                        break
                    if len(line) > 3:
                        addr_lines.append(line)
                    if len(addr_lines) >= 2:
                        break
                if addr_lines:
                    mailing = ", ".join(addr_lines)

        if prop:
            prop = " ".join(prop.strip().split())
        if mailing:
            mailing = " ".join(mailing.strip().split())

        return {"property_address": prop, "mailing_address": mailing}

    except Exception:
        return {"property_address": None, "mailing_address": None}
