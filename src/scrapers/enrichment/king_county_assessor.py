"""King County mailing address enrichment via payment.kingcounty.gov.

Simple: navigate to tax page, find "Mailing Address", take next 2 lines.
10 parallel tabs. ~500 parcels in ~3 minutes.
"""

import asyncio

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.enrichment.king_assessor")

_TAX_URL = "https://payment.kingcounty.gov/Home/Index?app=PropertyTaxes&Search="
_PARALLEL = 3  # 3 tabs — reliable on Railway's memory limits

add_scrape_domain("payment.kingcounty.gov")


async def batch_enrich_king_county(
    parcel_ids: list[str],
) -> dict[str, dict[str, str | None]]:
    """Look up mailing addresses for King County parcels. 10 parallel tabs."""
    results: dict[str, dict[str, str | None]] = {}
    clean = list(dict.fromkeys(pid.strip() for pid in parcel_ids if pid and len(pid.strip()) >= 6))

    if not clean:
        return results

    _logger.info("King County mailing enrichment: %d parcels, %d tabs", len(clean), _PARALLEL)

    async with BridgeScraper() as scraper:
        pages = [scraper.page]
        for _ in range(_PARALLEL - 1):
            pages.append(await scraper._context.new_page())

        for batch_start in range(0, len(clean), _PARALLEL):
            batch = clean[batch_start:batch_start + _PARALLEL]

            if batch_start % 100 == 0:
                _logger.info("  %d / %d ...", batch_start, len(clean))

            tasks = [_lookup(pages[i], pid) for i, pid in enumerate(batch)]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for pid, res in zip(batch, batch_results):
                if isinstance(res, dict) and res.get("mailing_address"):
                    results[pid] = res

            await asyncio.sleep(0.3)

        for p in pages[1:]:
            await p.close()

    _logger.info("Mailing enrichment done: %d/%d found", len(results), len(clean))
    return results


async def _lookup(page, parcel_id: str) -> dict[str, str | None]:
    """Navigate to tax page, extract mailing address. Simple string parsing."""
    try:
        await page.goto(f"{_TAX_URL}{parcel_id}", wait_until="load", timeout=15_000)

        # Wait for Account Summary to render
        try:
            await page.wait_for_function(
                "() => document.body.innerText.includes('Mailing Address') || document.body.innerText.includes('No accounts')",
                timeout=8_000,
            )
        except Exception:
            pass

        body = await page.inner_text("body")

        # Extract mailing address: find "Mailing Address", take next 2 non-empty lines
        mailing = None
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

        # Property address = first line of mailing (street only)
        prop = None
        if mailing:
            prop = mailing.split(",")[0].strip()

        return {"property_address": prop, "mailing_address": mailing}

    except Exception:
        return {"property_address": None, "mailing_address": None}
