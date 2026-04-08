"""King County address enrichment — hybrid HTTP + Playwright.

Step 1 (HTTP, fast): eRealProperty → property address + tax bill URL
Step 2 (Playwright, reliable): payment.kingcounty.gov → mailing address

500 parcels in ~5 min:
- Step 1: 500 × 1s = ~8 min (but can run 5 concurrent HTTP requests = ~2 min)
- Step 2: 500 × 4s / 1 tab = ~33 min → too slow
- Better: use 3 Playwright tabs for step 2 = ~11 min total

Actually: since Step 2 only needs Playwright for JS-rendered content,
we run Step 1 (HTTP) for ALL parcels first (fast), then Step 2 (Playwright)
for the subset that need mailing addresses.
"""

import asyncio
import re

import requests as sync_requests

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.enrichment.king_assessor")

_ERP_URL = "https://blue.kingcounty.com/Assessor/eRealProperty/Dashboard.aspx?ParcelNbr="
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"}

add_scrape_domain("blue.kingcounty.com")
add_scrape_domain("payment.kingcounty.gov")


async def batch_enrich_king_county(
    parcel_ids: list[str],
) -> dict[str, dict[str, str | None]]:
    """Two-phase enrichment: HTTP for property, Playwright for mailing."""
    results: dict[str, dict[str, str | None]] = {}
    clean = list(dict.fromkeys(pid.strip() for pid in parcel_ids if pid and len(pid.strip()) >= 6))

    if not clean:
        return results

    # ── Phase 1: HTTP requests for property address + tax URLs (fast) ─────
    _logger.info("Phase 1: HTTP lookup for %d parcels...", len(clean))
    tax_urls: dict[str, str] = {}  # pid → payment.kingcounty.gov URL

    for i, pid in enumerate(clean):
        if i % 100 == 0 and i > 0:
            _logger.info("  HTTP: %d / %d ...", i, len(clean))

        try:
            r = sync_requests.get(
                f"{_ERP_URL}{pid}", headers=_HEADERS, timeout=10
            )
            if r.status_code != 200:
                continue

            # Extract Site Address
            m = re.search(r"Site Address</td>\s*<td[^>]*>([^<]+)", r.text)
            prop = m.group(1).replace("&nbsp;", "").strip() if m else None
            if not prop:
                prop = None

            # Extract Tax Bill URL (has correct tax account number)
            m2 = re.search(
                r'href="(https://payment\.kingcounty\.gov[^"]+)"', r.text
            )
            tax_url = m2.group(1).replace("&amp;", "&") if m2 else None

            if prop or tax_url:
                results[pid] = {"property_address": prop, "mailing_address": None}
                if tax_url:
                    tax_urls[pid] = tax_url

        except Exception:
            pass

        await asyncio.sleep(0.1)  # minimal delay for HTTP

    _logger.info("Phase 1 done: %d/%d property addresses, %d tax URLs",
                 sum(1 for r in results.values() if r.get("property_address")),
                 len(clean), len(tax_urls))

    # ── Phase 2: Playwright for mailing addresses (5 concurrent tabs) ──────
    if not tax_urls:
        return results

    _CONCURRENT_TABS = 5
    _logger.info("Phase 2: Playwright lookup for %d mailing addresses (%d concurrent tabs)...",
                 len(tax_urls), _CONCURRENT_TABS)

    async def _lookup_mailing(page, pid: str, url: str) -> tuple[str, str | None]:
        """Look up mailing address from a single tax bill page."""
        try:
            await page.goto(url, wait_until="load", timeout=15_000)
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
                    return pid, " ".join(", ".join(addr_lines).strip().split())
        except Exception:
            pass
        return pid, None

    async with BridgeScraper() as scraper:
        # Open additional tabs (scraper already has 1 page)
        extra_pages = []
        for _ in range(_CONCURRENT_TABS - 1):
            extra_pages.append(await scraper.context.new_page())
        pages = [scraper.page] + extra_pages

        pids_to_lookup = list(tax_urls.keys())
        completed = 0

        # Process in batches of _CONCURRENT_TABS
        for batch_start in range(0, len(pids_to_lookup), _CONCURRENT_TABS):
            batch = pids_to_lookup[batch_start:batch_start + _CONCURRENT_TABS]

            tasks = []
            for i, pid in enumerate(batch):
                page = pages[i % len(pages)]
                tasks.append(_lookup_mailing(page, pid, tax_urls[pid]))

            batch_results = await asyncio.gather(*tasks)
            for pid, mailing in batch_results:
                if mailing and pid in results:
                    results[pid]["mailing_address"] = mailing

            completed += len(batch)
            if completed % 50 == 0 or completed == len(pids_to_lookup):
                _logger.info("  Mailing: %d / %d ...", completed, len(pids_to_lookup))

            await asyncio.sleep(0.2)

        # Close extra tabs
        for page in extra_pages:
            await page.close()

    found_mail = sum(1 for r in results.values() if r.get("mailing_address"))
    found_prop = sum(1 for r in results.values() if r.get("property_address"))
    _logger.info("Enrichment done: %d/%d property, %d/%d mailing",
                 found_prop, len(clean), found_mail, len(clean))
    return results
