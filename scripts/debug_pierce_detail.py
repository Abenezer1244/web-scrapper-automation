"""Step-by-step probe of Pierce ARMS detail page flow to find the broken selector.

Navigates to the search results, clicks into first instrument, then tests
each selector independently: dropdown, Legal Description tab, Parcel Id cell.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PLAYWRIGHT_HEADLESS", "true")


async def main() -> int:
    from src.scrapers.pierce_wa_probate import PierceWAARMSScraper

    async with PierceWAARMSScraper(record_type="probate") as s:
        # Run through the scrape setup (disclaimer + search) but stop
        # before parcel extraction — we want to probe from the results state.
        await s._accept_disclaimer()
        await s.navigate("https://armsweb.co.pierce.wa.us/RealEstate/SearchEntry.aspx")
        await s._fill_search_form("03/01/2026", "03/08/2026")

        page = s.page
        await page.wait_for_timeout(2000)
        print(f"\n[1] After search: url={page.url}", flush=True)
        print(f"    Title: {await page.title()}", flush=True)

        # Find first instrument cell
        first_inst = await page.evaluate(r"""() => {
            const cells = document.querySelectorAll('td.fauxDetailLink');
            for (const c of cells) {
                if (/^\d{10,12}$/.test(c.textContent.trim())) return c.textContent.trim();
            }
            // Fallback: ANY fauxDetailLink
            if (cells.length > 0) return cells[0].textContent.trim();
            return null;
        }""")
        print(f"\n[2] First instrument candidate: {first_inst!r}", flush=True)

        # Also count ALL fauxDetailLink cells
        all_links = await page.evaluate(r"""() => {
            const cells = document.querySelectorAll('td.fauxDetailLink');
            return Array.from(cells).slice(0, 5).map(c => c.textContent.trim());
        }""")
        print(f"    First 5 fauxDetailLink cells: {all_links}", flush=True)

        if not first_inst:
            # Maybe the class name changed — look for any cell linking to a detail page
            other_links = await page.evaluate("""() => {
                const links = document.querySelectorAll('a[href*="DetailsOf"], a[href*="Detail"], td[onclick*="Detail"]');
                return Array.from(links).slice(0, 5).map(l => ({
                    tag: l.tagName,
                    cls: l.className,
                    text: l.textContent.trim().slice(0, 40),
                    href: l.getAttribute('href') || l.getAttribute('onclick') || '',
                }));
            }""")
            print(f"    No fauxDetailLink — alternate candidates: {other_links}", flush=True)
            return 1

        # Click it
        print(f"\n[3] Clicking '{first_inst}'...", flush=True)
        try:
            await page.locator(f"td.fauxDetailLink:has-text('{first_inst}')").first.click(timeout=5_000)
            await page.wait_for_load_state("load")
            await page.wait_for_timeout(1500)
            print(f"    After click: url={page.url}", flush=True)
            print(f"    Title: {await page.title()}", flush=True)
        except Exception as exc:
            print(f"    CLICK FAILED: {exc}", flush=True)
            return 1

        # Find instrument dropdown
        dropdown_info = await page.evaluate("""() => {
            // Try multiple selector candidates
            const selectors = [
                '#cphNoMargin_OptionsBar1_ItemList',
                '#cphNoMargin_cphNoMargin_OptionsBar1_ItemList',
                'select[id*="OptionsBar"]',
                'select[id*="ItemList"]',
            ];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el) return {found: true, selector: sel, id: el.id, options: el.options.length};
            }
            // Dump all selects
            const all = document.querySelectorAll('select');
            return {found: false, total_selects: all.length, select_ids: Array.from(all).map(s => s.id).slice(0, 10)};
        }""")
        print(f"\n[4] Instrument dropdown: {dropdown_info}", flush=True)

        # Check for Legal Description tab
        legal_info = await page.evaluate("""() => {
            // Try various selectors for the Legal Description tab
            const candidates = [];
            document.querySelectorAll('span, a, li, button, div').forEach(el => {
                const t = el.textContent.trim();
                if (t === 'Legal Description' || t === 'Legal Descriptions') {
                    candidates.push({tag: el.tagName, cls: el.className, id: el.id});
                }
            });
            return {candidates: candidates.slice(0, 5)};
        }""")
        print(f"\n[5] Legal Description tab candidates: {legal_info}", flush=True)

        # If tab exists, click it and look for Parcel Id
        if legal_info["candidates"]:
            try:
                await page.locator("span:has-text('Legal Description'), a:has-text('Legal Description'), li:has-text('Legal Description')").first.click(timeout=5_000)
                await page.wait_for_timeout(1000)
                print("    Clicked Legal Description tab", flush=True)
            except Exception as exc:
                print(f"    Click failed: {exc}", flush=True)

        parcel_search = await page.evaluate(r"""() => {
            const out = {parcel_id_cells: [], all_parcel_text: []};
            // Look for td with "Parcel Id:" or "Parcel ID:"
            document.querySelectorAll('td, th, span, label, div').forEach(el => {
                const t = el.textContent.trim();
                if (/^Parcel\s*Id:?$/i.test(t) || t === 'Parcel ID:' || t === 'Parcel Id:') {
                    // Find the value — next sibling cell or next td
                    let val = '';
                    if (el.nextElementSibling) val = el.nextElementSibling.textContent.trim();
                    out.parcel_id_cells.push({label: t, value: val.slice(0, 50), tag: el.tagName});
                }
                // Also capture any node whose text starts with "Parcel"
                if (t.toLowerCase().startsWith('parcel') && t.length < 120 && !t.includes('description')) {
                    out.all_parcel_text.push(t.slice(0, 100));
                }
            });
            return {
                parcel_id_cells: out.parcel_id_cells.slice(0, 5),
                all_parcel_text: [...new Set(out.all_parcel_text)].slice(0, 10),
            };
        }""")
        print("\n[6] Parcel search results:", flush=True)
        print(f"    exact 'Parcel Id:' cells: {parcel_search['parcel_id_cells']}", flush=True)
        print("    any 'Parcel...' text:", flush=True)
        for t in parcel_search["all_parcel_text"]:
            print(f"      {t!r}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
