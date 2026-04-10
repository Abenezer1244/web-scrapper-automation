"""Dump Kitsap result table headers + every cell for probate-matching rows."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PLAYWRIGHT_HEADLESS", "true")


async def main() -> int:
    from src.scrapers.templates.eagleweb import EagleWebScraper

    async with EagleWebScraper(
        base_url="https://kcwaimg.kitsap.gov/recorder/web/",
        county="Kitsap",
        state="WA",
        record_types=["probate"],
    ) as scraper:
        await scraper.navigate(scraper.base_url)
        await scraper._accept_disclaimer()
        await scraper._configure_search("probate", "03/01/2026", "03/15/2026")
        await scraper._submit_search()

        result = await scraper.page.evaluate("""
            (() => {
                const tables = document.querySelectorAll('table');
                let best = null, bestRows = 0;
                for (const t of tables) {
                    const ths = Array.from(t.querySelectorAll('th')).map(h => h.textContent.trim().toUpperCase());
                    if (ths.includes('DESCRIPTION') || ths.includes('SUMMARY')) {
                        const rows = t.querySelectorAll('tr').length;
                        if (rows > bestRows) { bestRows = rows; best = t; }
                    }
                }
                if (!best) return {headers: [], rows: []};

                const headers = Array.from(best.querySelectorAll('th')).map(h => h.textContent.trim());
                const rows = best.querySelectorAll('tr');
                const out = [];
                const probateKeywords = ['PROBATE','LETTERS','PERSONAL REP','DEATH','HEIRSHIP','TRANSFER ON DEATH','WILL'];
                for (let i = 1; i < rows.length; i++) {
                    const cells = rows[i].querySelectorAll('td');
                    if (cells.length < 2) continue;
                    const desc = cells[0].textContent.trim().toUpperCase();
                    const isProbate = probateKeywords.some(k => desc.includes(k));
                    if (!isProbate) continue;
                    const cellTexts = Array.from(cells).map(c => c.textContent.trim().replace(/\\s+/g, ' '));
                    out.push(cellTexts);
                    if (out.length >= 5) break;
                }
                return {headers, rows: out};
            })()
        """)

        print(f"\nHeaders ({len(result['headers'])}): {result['headers']}\n", flush=True)
        for i, row in enumerate(result["rows"]):
            print(f"--- Probate row {i} ({len(row)} cells) ---", flush=True)
            for j, cell in enumerate(row):
                hdr = result["headers"][j] if j < len(result["headers"]) else f"(col{j})"
                print(f"  [{j}] {hdr}: {cell}", flush=True)
            print("", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
