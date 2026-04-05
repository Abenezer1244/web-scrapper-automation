"""King County eRealProperty assessor enrichment.

Looks up parcel IDs on blue.kingcounty.com/Assessor/eRealProperty/ to get:
- Property address (from search results)
- Mailing address (from Property Tax Bill page)

Flow:
1. Navigate to eRealProperty
2. Check acknowledgment checkbox (#cphContent_checkbox_acknowledge)
3. Search form appears — enter parcel ID
4. Property address shows in results
5. Click "Property Tax Bill" → mailing address under owner name

Cost: $0 — free county website, no API key needed.
Requires: Playwright (navigates ASP.NET pages).
"""

import asyncio

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.enrichment.king_assessor")

_BASE_URL = "https://blue.kingcounty.com/Assessor/eRealProperty/default.aspx"

add_scrape_domain("blue.kingcounty.com")


async def batch_enrich_king_county(
    parcel_ids: list[str],
) -> dict[str, dict[str, str | None]]:
    """Look up multiple King County parcels via eRealProperty.

    Opens one browser, accepts disclaimer once, then searches each parcel.

    Returns:
        Dict mapping parcel_id → {property_address, mailing_address}.
    """
    results: dict[str, dict[str, str | None]] = {}

    if not parcel_ids:
        return results

    _logger.info("King County eRealProperty enrichment: %d parcels", len(parcel_ids))

    async with BridgeScraper() as scraper:
        # Navigate and accept disclaimer
        await scraper.navigate(_BASE_URL)
        await _accept_disclaimer(scraper)

        for i, pid in enumerate(parcel_ids, 1):
            pid = pid.strip()
            if not pid or len(pid) < 6:
                continue

            _logger.info("Parcel %d/%d: %s", i, len(parcel_ids), pid)

            try:
                result = await _lookup_parcel(scraper, pid)
                if result.get("property_address"):
                    results[pid] = result
                    _logger.info("  → %s | mail: %s",
                                 result.get("property_address", ""),
                                 (result.get("mailing_address") or "")[:60])
                else:
                    _logger.info("  → no data")
            except Exception as exc:
                _logger.warning("  → error: %s", str(exc)[:80])

            # Polite delay between lookups
            await asyncio.sleep(2)

    _logger.info("eRealProperty enrichment: %d/%d found", len(results), len(parcel_ids))
    return results


async def _accept_disclaimer(scraper: BridgeScraper) -> None:
    """Click the acknowledgment checkbox to reveal the search form.

    ASP.NET postback fires on click — must use Playwright click (not JS check).
    """
    await scraper.page.wait_for_timeout(2000)

    try:
        checkbox = scraper.page.locator("#cphContent_checkbox_acknowledge")
        if await checkbox.count() > 0 and await checkbox.is_visible():
            await checkbox.scroll_into_view_if_needed()
            await scraper.page.wait_for_timeout(500)
            await checkbox.click()  # Must use click (not check) for ASP.NET postback
            await scraper.page.wait_for_timeout(4000)
            _logger.info("Acknowledgment checkbox clicked — search form loaded")
        else:
            # Check if search form is already visible (checkbox already accepted)
            parcel_input = scraper.page.locator("#cphContent_txtParcelNbr")
            if await parcel_input.count() > 0:
                _logger.info("Search form already visible — disclaimer already accepted")
            else:
                _logger.info("No checkbox found and no search form")
    except Exception as exc:
        _logger.warning("Disclaimer error: %s", str(exc)[:80])


async def _lookup_parcel(scraper: BridgeScraper, parcel_id: str) -> dict[str, str | None]:
    """Search for a parcel ID and extract property + mailing address."""
    empty = {"property_address": None, "mailing_address": None}

    # The parcel search input: #cphContent_txtParcelNbr
    search_input = scraper.page.locator("#cphContent_txtParcelNbr")

    if await search_input.count() == 0:
        # Navigate back and re-accept disclaimer
        await scraper.page.goto(_BASE_URL, wait_until="load", timeout=30_000)
        await _accept_disclaimer(scraper)
        search_input = scraper.page.locator("#cphContent_txtParcelNbr")

    if await search_input.count() == 0:
        _logger.warning("Parcel search input #cphContent_txtParcelNbr not found")
        return empty

    # Enter parcel ID (10 digits, no hyphens)
    await search_input.click()
    await search_input.fill("")
    await search_input.press_sequentially(parcel_id, delay=30)

    # Click the Search button for parcel number
    search_btn = scraper.page.locator("#cphContent_btn_Search")
    if await search_btn.count() > 0:
        await search_btn.click()
    else:
        await search_input.press("Enter")

    # Wait for ASP.NET postback to load results
    await scraper.page.wait_for_timeout(5000)

    # Extract property address from the results page
    property_address = await scraper.page.evaluate("""
        (() => {
            // Look for address in table rows or labeled fields
            const body = document.body.innerText || '';

            // Common ASP.NET patterns for eRealProperty
            const patterns = [
                /(?:Property Address|Site Address|Location)[:\\s]*\\n?\\s*(.+?)(?:\\n|$)/im,
                /(?:Address)[:\\s]*\\n?\\s*(\\d+.+?)(?:\\n|$)/im,
            ];
            for (const p of patterns) {
                const m = body.match(p);
                if (m && m[1] && m[1].trim().length > 5 && !m[1].includes('Search')) {
                    return m[1].trim();
                }
            }

            // Look in spans/labels with property address IDs
            const addrEl = document.querySelector(
                '#cphContent_lblAddress, #cphContent_lblSiteAddress, ' +
                '[id*="Address"]:not(input), [id*="SiteAddr"]'
            );
            if (addrEl) {
                const text = addrEl.textContent.trim();
                if (text.length > 5) return text;
            }

            // Try table cells — look for row with "Address" label
            const tds = document.querySelectorAll('td, th');
            for (let i = 0; i < tds.length; i++) {
                const text = tds[i].textContent.trim();
                if (/^(?:Property |Site )?Address$/i.test(text)) {
                    // Next sibling cell or next td
                    const next = tds[i].nextElementSibling || tds[i + 1];
                    if (next) {
                        const addr = next.textContent.trim();
                        if (addr.length > 5) return addr;
                    }
                }
            }

            return null;
        })()
    """)

    if not property_address:
        # Log what's on the page for debugging
        page_text = await scraper.page.inner_text("body")
        _logger.info("No property address found. Page title: %s, text (300): %s",
                      await scraper.page.title(), page_text[:300].replace('\n', ' '))
        return empty

    property_address = " ".join(property_address.strip().split())
    _logger.info("Property address: %s", property_address)

    # Try to get mailing address from Property Tax Bill page
    mailing_address = await _get_mailing_address(scraper)

    return {
        "property_address": property_address,
        "mailing_address": mailing_address or property_address,
    }


async def _get_mailing_address(scraper: BridgeScraper) -> str | None:
    """Navigate to Property Tax Bill page and extract mailing address.

    The Tax Bill link goes to payment.kingcounty.gov — a separate site
    that shows the owner name and mailing address.
    """
    try:
        # The Tax Bill link: #cphContent_HyperLinkPropertyTaxInformationSystem
        # Goes to: payment.kingcounty.gov/Home/Index?app=PropertyTaxes&Search=PARCEL
        tax_link = scraper.page.locator(
            "#cphContent_HyperLinkPropertyTaxInformationSystem, "
            "a:has-text('Property Tax Bill')"
        )
        if await tax_link.count() == 0:
            _logger.info("No Property Tax Bill link found")
            return None

        # Get the href and navigate (it's a different domain)
        href = await tax_link.first.get_attribute("href")
        if not href:
            return None

        add_scrape_domain("payment.kingcounty.gov")
        await scraper.page.goto(href, wait_until="domcontentloaded", timeout=30_000)
        await scraper.page.wait_for_timeout(5000)

        # The payment page shows owner info + mailing address
        mailing = await scraper.page.evaluate("""
            (() => {
                const body = document.body.innerText || '';

                // Look for mailing address patterns on the tax bill page
                const patterns = [
                    /Mailing Address[:\\s]*\\n?\\s*(.+?)(?:\\n\\n|\\n[A-Z]|$)/im,
                    /Mail(?:ing)?[:\\s]+(.+?)(?:\\n\\n|$)/im,
                ];
                for (const p of patterns) {
                    const m = body.match(p);
                    if (m && m[1] && m[1].trim().length > 5) return m[1].trim();
                }

                // Look in any element with "mailing" in id/class
                const els = document.querySelectorAll('[id*="mail" i], [class*="mail" i], [id*="Mail"], [class*="Mail"]');
                for (const el of els) {
                    const text = el.textContent.trim();
                    if (text.length > 5 && text.length < 200) return text;
                }

                // Look for address pattern after owner name
                // Tax bill pages typically show: Owner Name \\n Address Line 1 \\n City State Zip
                const lines = body.split('\\n').map(l => l.trim()).filter(l => l.length > 0);
                for (let i = 0; i < lines.length; i++) {
                    if (/Taxpayer|Owner|Name/i.test(lines[i])) {
                        // Next 2-3 lines might be the address
                        const addr = [];
                        for (let j = i + 1; j < Math.min(i + 4, lines.length); j++) {
                            const line = lines[j].trim();
                            if (line.length > 3 && line.length < 100 && !/Taxpayer|Owner|Parcel|Account|Tax Year/i.test(line)) {
                                addr.push(line);
                            } else {
                                break;
                            }
                        }
                        if (addr.length >= 1) return addr.join(', ');
                    }
                }

                return null;
            })()
        """)

        if mailing:
            mailing = " ".join(mailing.strip().split())
            _logger.info("Mailing address: %s", mailing)
            return mailing

        # Log page text for debugging
        text = await scraper.page.inner_text("body")
        _logger.info("No mailing found. Tax bill text (300): %s", text[:300].replace('\n', ' '))

    except Exception as exc:
        _logger.info("Tax bill page: %s", str(exc)[:80])

    return None
