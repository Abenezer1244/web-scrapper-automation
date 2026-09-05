"""Compare the CURRENT results-table picker against the PROPOSED one across days
with 0 / 3 / 5 / many records, for all three Pierce ARMS record types.

    railway run --service worker python scripts/diag_test11_predicate.py
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.ERROR)

CASES = [
    ("pre_foreclosure", "05/26/2026", "05/26/2026"),   # 3 records  -> currently RAISES
    ("pre_foreclosure", "08/24/2026", "08/24/2026"),   # 5 records
    ("pre_foreclosure", "08/30/2026", "08/30/2026"),   # 0 records
    ("pre_foreclosure", "06/04/2026", "09/01/2026"),   # 228 -> 10 pages, last page 3 rows
    ("probate", "08/24/2026", "08/28/2026"),
    ("divorce", "08/24/2026", "08/28/2026"),
]


def pick_current(soup):
    for t in soup.find_all("table"):
        rows = t.find_all("tr")
        if len(rows) < 5:
            continue
        if len(rows) > 1:
            ftd = rows[1].find("td")
            if ftd and ftd.get_text(strip=True).isdigit():
                return t
    return None


def pick_proposed(soup):
    for t in soup.find_all("table"):
        rows = t.find_all("tr")
        if len(rows) < 2:
            continue
        cells = rows[1].find_all("td")
        if len(cells) < 9:
            continue
        if cells[0].get_text(strip=True).isdigit():
            return t
    return None


def _n(t):
    return "None" if t is None else f"tr={len(t.find_all('tr'))}"


async def main():
    from src.scrapers.pierce_wa_probate import (
        PierceWADivorceScraper,
        PierceWAPreForeclosureScraper,
        PierceWAProbateScraper,
    )
    klass = {
        "pre_foreclosure": PierceWAPreForeclosureScraper,
        "probate": PierceWAProbateScraper,
        "divorce": PierceWADivorceScraper,
    }
    for rt, d_from, d_to in CASES:
        scraper = klass[rt]()
        async with scraper:
            await scraper._accept_disclaimer()
            await scraper.navigate("https://armsweb.co.pierce.wa.us/RealEstate/SearchEntry.aspx")
            await scraper._fill_search_form(d_from, d_to)
            soup = await scraper.get_soup_async()
            cur, prop = pick_current(soup), pick_proposed(soup)
            same = "SAME" if cur is prop else "DIFFER"
            print(f"{rt:16} {d_from}-{d_to}  marker={getattr(scraper, '_record_count', '?')!r:6} "
                  f"pages={getattr(scraper, '_page_total', '?'):3}  current={_n(cur):9} "
                  f"proposed={_n(prop):9} {same}", flush=True)


asyncio.run(main())
