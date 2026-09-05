"""Reproduce the Test 11 Pierce pre_foreclosure failure with the EXACT job date range.

    railway run --service worker python scripts/diag_test11_repro.py [date_from] [date_to]
"""
import asyncio
import logging
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

DATE_FROM = sys.argv[1] if len(sys.argv) > 1 else "06/04/2026"
DATE_TO = sys.argv[2] if len(sys.argv) > 2 else "09/02/2026"


def _on_progress(page_current, page_total, record_count, phase="scraping"):
    print(f"  PROGRESS page={page_current}/{page_total} records={record_count} phase={phase}",
          flush=True)


async def main():
    from src.scrapers.pierce_wa_probate import PierceWAPreForeclosureScraper

    scraper = PierceWAPreForeclosureScraper()
    scraper.on_progress = _on_progress
    print(f"=== REPRO Pierce pre_foreclosure {DATE_FROM} -> {DATE_TO} ===", flush=True)
    try:
        async with scraper:
            records = await scraper.scrape(DATE_FROM, DATE_TO)
        print(f"=== SUCCESS: {len(records)} records ===")
    except BaseException as exc:  # noqa: BLE001 - we want the raw class + trace
        print(f"\n=== FAILED: {type(exc).__module__}.{type(exc).__name__}: {exc} ===")
        traceback.print_exc()
        try:
            from src.scrapers.reliability import is_transient_scrape_error
            print("is_transient_scrape_error =", is_transient_scrape_error(exc))
        except Exception as e2:
            print("classify failed:", e2)


asyncio.run(main())
