"""Test Pierce County pre-foreclosure scraper (ARMS Web portal).

Pierce exposes 4 pre-foreclosure document types via ARMS checkboxes: Notice of
Default, Notice of Foreclosure, Lis Pendens, Notice of Trustee Sale. With no
doc_types selection the scraper searches all 4; pass doc_types to narrow.

Run: python scripts/test_pierce_preforeclosure.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PLAYWRIGHT_HEADLESS", "false")


async def main():
    from datetime import datetime, timedelta

    from src.scrapers.pierce_wa_probate import PierceWAPreForeclosureScraper

    end = datetime.now()
    start = end - timedelta(days=180)
    date_from = start.strftime("%m/%d/%Y")
    date_to = end.strftime("%m/%d/%Y")

    print("\n=== Pierce County PRE-FORECLOSURE (ARMS) ===")
    print(f"Date range: {date_from} to {date_to}")
    print()

    # record_type is pinned to "pre_foreclosure" by the subclass. doc_types=None
    # = all 4 pre-foreclosure ARMS checkboxes (NOD / Notice of Foreclosure /
    # Lis Pendens / Notice of Trustee Sale). See src/scrapers/doc_types.py.
    async with PierceWAPreForeclosureScraper() as scraper:
        print(f"Label: {scraper.DOC_TYPE_LABEL} | checkbox ids: {scraper.DOC_TYPE_IDS}\n")
        records = await scraper.scrape(date_from, date_to)

    print(f"\n=== RESULTS: {len(records)} records ===\n")
    for i, r in enumerate(records[:20], 1):
        print(f"  {i}. {r.date_recorded} | {r.party_name}")
        print(f"     Parcel: {r.parcel_id} | Recording#: {r.legal_description}")
        print(f"     Doc: {r.doc_type}\n")
    if len(records) > 20:
        print(f"  ... and {len(records) - 20} more")

    with_pid = len([r for r in records if r.parcel_id])
    print(f"\nTotal: {len(records)} records, {with_pid} with parcel IDs")


if __name__ == "__main__":
    asyncio.run(main())
