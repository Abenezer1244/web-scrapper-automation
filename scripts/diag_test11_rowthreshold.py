"""Search-only probe: for each day, report the ARMS record-count marker, the row
counts of every candidate table, and whether _extract_records() raises.

    railway run --service worker python scripts/diag_test11_rowthreshold.py MM/DD/YYYY ...
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.ERROR)
for name in ("scraper.pierce_wa_probate", "scraper.base", "scraper.enrichment.gis"):
    logging.getLogger(name).setLevel(logging.ERROR)

DAYS = sys.argv[1:]


async def main():
    from src.scrapers.pierce_wa_probate import PierceWAPreForeclosureScraper

    for day in DAYS:
        scraper = PierceWAPreForeclosureScraper()
        try:
            async with scraper:
                await scraper._accept_disclaimer()
                await scraper.navigate("https://armsweb.co.pierce.wa.us/RealEstate/SearchEntry.aspx")
                await scraper._fill_search_form(day, day)
                soup = await scraper.get_soup_async()
                marker = getattr(scraper, "_record_count", "?")
                tbl_rows = sorted(len(t.find_all("tr")) for t in soup.find_all("table"))
                try:
                    recs = scraper._extract_records(soup)
                    outcome = f"extracted={len(recs)}"
                except BaseException as exc:  # noqa: BLE001
                    outcome = f"RAISED {type(exc).__name__}: {str(exc)[:120]}"
                print(f"{day}  marker={marker!r} pages={getattr(scraper, '_page_total', '?')} "
                      f"table_tr_counts={tbl_rows[-6:]}  {outcome}", flush=True)
        except BaseException as exc:  # noqa: BLE001
            print(f"{day}  PROBE-ERROR {type(exc).__name__}: {str(exc)[:160]}", flush=True)


asyncio.run(main())
