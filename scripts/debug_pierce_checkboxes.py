"""Dump all Pierce ARMS checkbox IDs + labels so we can verify pre_foreclosure IDs."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PLAYWRIGHT_HEADLESS", "true")


async def main() -> int:
    from src.scrapers.pierce_wa_probate import PierceWAARMSScraper

    _ARMS_SEARCH = "https://armsweb.co.pierce.wa.us/RealEstate/SearchEntry.aspx"

    async with PierceWAARMSScraper(record_type="probate") as scraper:
        # Use scraper's own disclaimer accept flow
        await scraper._accept_disclaimer()
        await scraper.page.wait_for_timeout(2000)

        # Now navigate to search page if not already
        if "SearchEntry" not in scraper.page.url:
            await scraper.page.goto(_ARMS_SEARCH, wait_until="domcontentloaded", timeout=30_000)
        await scraper.page.wait_for_timeout(3000)
        await scraper.page.wait_for_timeout(2000)

        # Accept disclaimer if present
        disclaimer = scraper.page.locator("input[type='submit'][value*='ccept' i], button:has-text('Accept')")
        if await disclaimer.count() > 0:
            try:
                async with scraper.page.expect_navigation(timeout=10_000):
                    await disclaimer.first.click()
            except Exception:
                pass
            await scraper.page.wait_for_timeout(2000)

        print(f"URL: {scraper.page.url}", flush=True)

        # Click the Document Type tab if needed
        for tab_text in ["Document Type", "Real Property"]:
            tab = scraper.page.locator(f"a:has-text('{tab_text}'), [id*='DocType']")
            if await tab.count() > 0:
                try:
                    await tab.first.click()
                    await scraper.page.wait_for_timeout(1500)
                    break
                except Exception:
                    pass

        # Dump all checkboxes with IDs containing 'dclDocType'
        checkboxes = await scraper.page.evaluate("""
            () => {
                const out = [];
                document.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                    if (!cb.id) return;
                    if (!cb.id.includes('dclDocType')) return;
                    // Find label text
                    let label = '';
                    const labelEl = document.querySelector(`label[for="${cb.id}"]`);
                    if (labelEl) {
                        label = labelEl.textContent.trim();
                    } else {
                        const parent = cb.parentElement;
                        if (parent) label = parent.textContent.trim();
                    }
                    out.push({id: cb.id, label: label.slice(0, 80)});
                });
                return out;
            }
        """)

        print(f"\nFound {len(checkboxes)} doc-type checkboxes:", flush=True)
        # Filter for pre-foreclosure keywords
        pf_keywords = ["TRUSTEE", "LIS PENDEN", "FORECLOS", "DEFAULT", "NOD", "NOTICE OF"]
        probate_keywords = ["PROBATE", "DEATH", "TRANSFER ON DEATH"]

        print("\n--- PRE-FORECLOSURE candidates ---", flush=True)
        for cb in checkboxes:
            if any(k in cb["label"].upper() for k in pf_keywords):
                # Extract numeric ID from end of element ID
                id_num = cb["id"].split("_")[-1]
                print(f"  id={id_num:>6s} ({cb['id']}) label={cb['label']!r}", flush=True)

        print("\n--- PROBATE (for comparison) ---", flush=True)
        for cb in checkboxes:
            if any(k in cb["label"].upper() for k in probate_keywords):
                id_num = cb["id"].split("_")[-1]
                print(f"  id={id_num:>6s} ({cb['id']}) label={cb['label']!r}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
