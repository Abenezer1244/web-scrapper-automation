"""Probe Whatcom Advanced Search: dropdown options + test submit."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PLAYWRIGHT_HEADLESS", "true")


async def main() -> int:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()

        # Accept disclaimer
        await page.goto("https://recording.whatcomcounty.us/", timeout=30_000)
        btn = page.locator("button:has-text('Agree'), input[type='submit'][value*='Agree' i]")
        if await btn.count() > 0:
            try:
                async with page.expect_navigation(timeout=10_000):
                    await btn.first.click()
            except Exception:
                pass

        # Advanced search
        await page.goto("https://recording.whatcomcounty.us/?mode=Advanced", timeout=30_000)
        await page.wait_for_timeout(2000)

        # Dump subtype dropdown options
        subtype_opts = await page.evaluate("""
            () => {
                // Try a few likely selectors
                const sel = document.querySelector('#Filter_DocumentSubtype, select[name="Criteria.Filter.SubtypeKey"]');
                if (!sel) return {found: false, html: document.body.innerHTML.slice(0, 2000)};
                const opts = Array.from(sel.options || []).map(o => ({value: o.value, text: o.text}));
                return {found: true, tag: sel.tagName, opts};
            }
        """)
        print(f"\nSubtype dropdown: found={subtype_opts.get('found')}, count={len(subtype_opts.get('opts',[]))}", flush=True)
        if subtype_opts.get("found"):
            # Only show PROBATE-relevant entries
            keywords = ["PROBATE", "DEATH", "TRANSFER ON DEATH", "HEIRSHIP", "PERSONAL REP", "ESTATE", "WILL", "LETTERS"]
            print("\nProbate-relevant options:", flush=True)
            for o in subtype_opts["opts"]:
                t = o["text"].upper()
                if any(k in t for k in keywords):
                    print(f"  {o['value']!r:10s} {o['text']}", flush=True)
        else:
            # It might be a different widget (typeahead / autocomplete)
            print("Not a standard select — probably a typeahead", flush=True)

        # Fill dates and submit
        await page.fill("#Criteria_Filter_RecordingDateStart", "03/01/2026")
        await page.fill("#Criteria_Filter_RecordingDateEnd", "03/08/2026")

        # Find submit button
        submit = page.locator("button[type='submit'], input[type='submit']")
        print(f"\nSubmit buttons: {await submit.count()}", flush=True)
        for i in range(min(await submit.count(), 5)):
            b = submit.nth(i)
            txt = (await b.text_content() or "").strip() or (await b.get_attribute("value") or "")
            print(f"  [{i}] {txt!r}", flush=True)

        # Click the first Search button
        search_btn = page.locator("button:has-text('Search'), input[type='submit'][value*='Search' i]")
        if await search_btn.count() > 0:
            print("\nClicking Search...", flush=True)
            try:
                async with page.expect_navigation(timeout=20_000):
                    await search_btn.first.click()
            except Exception as e:
                print(f"nav timeout: {e}", flush=True)
                await page.wait_for_timeout(5000)

        print(f"\nAfter search URL: {page.url}", flush=True)
        print(f"Title: {await page.title()}", flush=True)

        # Wait for AJAX/JS-rendered results
        await page.wait_for_timeout(5000)

        # Dump result structure — could be tables OR divs
        containers = await page.evaluate("""
            () => {
                const out = {tables: [], candidates: []};
                document.querySelectorAll('table').forEach(t => {
                    const rows = t.querySelectorAll('tr').length;
                    if (rows > 0) {
                        out.tables.push({rows, headers: Array.from(t.querySelectorAll('th')).map(h => h.textContent.trim())});
                    }
                });
                // Find divs that contain many similar children (likely result cards)
                document.querySelectorAll('[id*="result" i], [class*="result" i], [id*="record" i], [class*="record" i]').forEach(el => {
                    out.candidates.push({
                        tag: el.tagName,
                        id: el.id,
                        cls: el.className.slice(0,80),
                        children: el.children.length,
                    });
                });
                // Also count message elements
                const body = document.body.innerText;
                out.bodySample = body.slice(0, 500);
                out.noResults = body.toLowerCase().includes('no results') || body.toLowerCase().includes('no records');
                return out;
            }
        """)
        print(f"\nTables: {len(containers['tables'])}", flush=True)
        for t in containers["tables"][:5]:
            print(f"  rows={t['rows']} headers={t['headers']}", flush=True)
        print("\nResult-like containers:", flush=True)
        for c in containers["candidates"][:10]:
            print(f"  {c}", flush=True)
        print(f"\nnoResults flag: {containers['noResults']}", flush=True)
        print(f"\nbody sample:\n{containers['bodySample']}", flush=True)

        # Find individual result cards — walk DOM for elements containing "APN#"
        cards = await page.evaluate("""
            () => {
                const all = document.querySelectorAll('div, article, section, li');
                const hits = [];
                for (const el of all) {
                    const t = el.textContent || '';
                    if (t.includes('APN#') && t.includes('GRANTOR') && t.length < 2000) {
                        // Likely a result card
                        hits.push({
                            tag: el.tagName,
                            id: el.id,
                            cls: (el.className || '').toString().slice(0, 80),
                            text: t.replace(/\\s+/g, ' ').slice(0, 400),
                            childCount: el.children.length,
                        });
                        if (hits.length >= 3) break;
                    }
                }
                return hits;
            }
        """)
        print(f"\nResult cards found ({len(cards)}):", flush=True)
        for i, c in enumerate(cards):
            print(f"  --- card {i} ---", flush=True)
            print(f"  tag={c['tag']} id={c['id']!r} cls={c['cls']!r} children={c['childCount']}", flush=True)
            print(f"  text: {c['text']}", flush=True)

        # First row of the biggest table
        first_row = await page.evaluate("""
            () => {
                let best = null, bestRows = 0;
                document.querySelectorAll('table').forEach(t => {
                    const r = t.querySelectorAll('tbody tr').length;
                    if (r > bestRows) { bestRows = r; best = t; }
                });
                if (!best) return null;
                const row = best.querySelector('tbody tr');
                if (!row) return null;
                return Array.from(row.querySelectorAll('td')).map(c => c.textContent.trim().slice(0, 150));
            }
        """)
        if first_row:
            print(f"\nFirst result row cells ({len(first_row)}):", flush=True)
            for i, c in enumerate(first_row):
                print(f"  [{i}] {c}", flush=True)

        await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
