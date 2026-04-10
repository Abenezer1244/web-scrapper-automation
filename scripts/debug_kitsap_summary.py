"""Dump raw Kitsap result-table rows so we can see exactly how parcels are formatted."""
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
        await scraper._configure_search("probate", "03/01/2026", "03/08/2026")
        await scraper._submit_search()

        raw = await scraper.page.evaluate("""
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
                if (!best) return [];
                const rows = best.querySelectorAll('tr');
                const out = [];
                for (let i = 1; i < Math.min(rows.length, 8); i++) {
                    const cells = rows[i].querySelectorAll('td');
                    if (cells.length < 2) continue;
                    out.push({
                        desc: cells[0].textContent.trim(),
                        summary: cells[1].textContent.trim(),
                        nCells: cells.length,
                    });
                }
                return out;
            })()
        """)

        for i, row in enumerate(raw):
            print(f"\n--- Row {i} ({row['nCells']} cells) ---", flush=True)
            print(f"DESC:    {row['desc'][:250]}", flush=True)
            print(f"SUMMARY: {row['summary'][:350]}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
