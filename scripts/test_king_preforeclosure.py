"""Test King County pre-foreclosure scraper (Notice of Trustee Sale).

Run: PLAYWRIGHT_HEADLESS=false python scripts/test_king_preforeclosure.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PLAYWRIGHT_HEADLESS", "false")


async def main():
    from datetime import datetime, timedelta

    from src.scrapers.king_wa_probate import KingWaPreForeclosureScraper

    end = datetime.now()
    start = end - timedelta(days=180)
    date_from = start.strftime("%m/%d/%Y")
    date_to = end.strftime("%m/%d/%Y")

    print("\n=== King County PRE-FORECLOSURE (Notice of Trustee Sale) ===")
    print(f"Date range: {date_from} to {date_to}")
    print()

    # record_type is pinned to "pre_foreclosure" by the subclass; doc_types
    # narrows the search to the Notice of Trustee Sale document type (the only
    # pre-foreclosure type King's recorder exposes — see src/scrapers/doc_types.py).
    async with KingWaPreForeclosureScraper(
        doc_types=["notice_of_trustee_sale"]
    ) as scraper:
        records = await scraper.scrape(date_from, date_to)

    print(f"\n=== RESULTS: {len(records)} records with Parcel IDs ===\n")

    for i, r in enumerate(records[:15], 1):
        print(f"  {i}. {r.date_recorded} | Borrower: {r.party_name}")
        print(f"     Lender: {r.heirs}")
        print(f"     Parcel: {r.parcel_id} | Recording: {r.legal_description}")
        print(f"     Doc: {r.doc_type}")
        print()

    if len(records) > 15:
        print(f"  ... and {len(records) - 15} more")

    # Stats
    with_pid = len([r for r in records if r.parcel_id])
    print(f"\nTotal: {len(records)} records, {with_pid} with parcel IDs")


if __name__ == "__main__":
    asyncio.run(main())
