"""Dump every <table>'s id/class/row-count/first-row shape on an ARMS results page,
so we can pick the results grid by a stable attribute instead of a row-count heuristic.

    railway run --service worker python scripts/diag_test11_tables.py MM/DD/YYYY [MM/DD/YYYY]
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.ERROR)

DAYS = sys.argv[1:] or ["05/26/2026", "08/24/2026", "08/30/2026"]


async def main():
    from src.scrapers.pierce_wa_probate import PierceWAPreForeclosureScraper

    for day in DAYS:
        scraper = PierceWAPreForeclosureScraper()
        async with scraper:
            await scraper._accept_disclaimer()
            await scraper.navigate("https://armsweb.co.pierce.wa.us/RealEstate/SearchEntry.aspx")
            await scraper._fill_search_form(day, day)
            soup = await scraper.get_soup_async()
            print(f"\n##### {day}  marker={getattr(scraper, '_record_count', '?')!r} #####")
            for t in soup.find_all("table"):
                rows = t.find_all("tr")
                tid = t.get("id") or ""
                cls = " ".join(t.get("class") or [])
                shape = ""
                if len(rows) > 1:
                    ftd = rows[1].find("td")
                    shape = f"row1_first_td={(ftd.get_text(strip=True)[:12] if ftd else None)!r} cells={len(rows[1].find_all('td'))}"
                elif rows:
                    shape = f"row0_cells={len(rows[0].find_all('td'))}"
                print(f"  id={tid!r:52} class={cls!r:22} tr={len(rows):3}  {shape}")


asyncio.run(main())
