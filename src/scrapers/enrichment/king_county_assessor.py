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

        except Exception as exc:
            _logger.debug(
                "Property URL fetch failed for parcel=%s: %s",
                pid, str(exc)[:200],
            )

        await asyncio.sleep(0.1)  # minimal delay for HTTP

    _logger.info("Phase 1 done: %d/%d property addresses, %d tax URLs",
                 sum(1 for r in results.values() if r.get("property_address")),
                 len(clean), len(tax_urls))

    # ── Phase 2: Playwright for mailing addresses ──────────────────────────
    if not tax_urls:
        return results

    # Cap at 200 parcels to avoid job timeout (~5-10s per lookup)
    _MAX_MAILING_LOOKUPS = 200
    pids_to_lookup = list(tax_urls.keys())
    if len(pids_to_lookup) > _MAX_MAILING_LOOKUPS:
        _logger.info("Capping mailing lookups: %d → %d (to avoid timeout)", len(pids_to_lookup), _MAX_MAILING_LOOKUPS)
        pids_to_lookup = pids_to_lookup[:_MAX_MAILING_LOOKUPS]

    _logger.info("Phase 2: Playwright lookup for %d mailing addresses...", len(pids_to_lookup))

    async with BridgeScraper() as scraper:

        for i, pid in enumerate(pids_to_lookup):
            if i % 25 == 0:
                _logger.info("  Mailing: %d / %d ...", i, len(pids_to_lookup))

            try:
                url = tax_urls[pid]
                await scraper.page.goto(url, wait_until="domcontentloaded", timeout=8_000)

                try:
                    await scraper.page.wait_for_function(
                        "() => document.body.innerText.includes('Mailing Address') || document.body.innerText.includes('No accounts')",
                        timeout=4_000,
                    )
                except Exception:
                    pass

                body = await scraper.page.inner_text("body")
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
                        mailing = " ".join(", ".join(addr_lines).strip().split())
                        results[pid]["mailing_address"] = mailing

            except Exception:
                pass

            await asyncio.sleep(0.2)

    found_mail = sum(1 for r in results.values() if r.get("mailing_address"))
    found_prop = sum(1 for r in results.values() if r.get("property_address"))
    _logger.info("Enrichment done: %d/%d property, %d/%d mailing",
                 found_prop, len(clean), found_mail, len(clean))
    return results
