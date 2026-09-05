"""Dump the raw ARMS results-table rows for a day so we can see which row(s)
_extract_records drops and why.

    railway run --service worker python scripts/diag_test11_dumprows.py MM/DD/YYYY
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.ERROR)

DAY = sys.argv[1] if len(sys.argv) > 1 else "08/24/2026"


async def main():
    from src.scrapers.pierce_wa_probate import PierceWAPreForeclosureScraper

    scraper = PierceWAPreForeclosureScraper()
    async with scraper:
        await scraper._accept_disclaimer()
        await scraper.navigate("https://armsweb.co.pierce.wa.us/RealEstate/SearchEntry.aspx")
        await scraper._fill_search_form(DAY, DAY)
        soup = await scraper.get_soup_async()
        print(f"marker={getattr(scraper, '_record_count', '?')!r}")
        # replicate the table pick
        for t in soup.find_all("table"):
            rows = t.find_all("tr")
            if len(rows) < 5:
                continue
            first_td = rows[1].find("td") if len(rows) > 1 else None
            if first_td and first_td.get_text(strip=True).isdigit():
                print(f"PICKED table with {len(rows)} tr")
                for i, row in enumerate(rows):
                    cells = row.find_all("td")
                    txts = [c.get_text(" ", strip=True)[:38] for c in cells]
                    first = txts[0] if txts else ""
                    rec = None
                    reason = ""
                    if i == 0:
                        reason = "row[0] skipped by rows[1:]"
                    elif len(cells) < 9:
                        reason = f"DROPPED len(cells)={len(cells)} < 9"
                    elif not first.isdigit():
                        reason = f"DROPPED first cell {first!r} not digit"
                    else:
                        rec = scraper._map_row(cells)
                        reason = "kept" if rec else "DROPPED _map_row returned None"
                    print(f"  tr[{i}] cells={len(cells)} {reason}")
                    print(f"        {txts}")
                break


asyncio.run(main())
