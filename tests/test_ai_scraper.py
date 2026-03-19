"""Quick test: AI scraper against King County, WA (Landmark Web).

This tests the full AI extraction pipeline:
1. Navigate to King County's public records site
2. Claude analyzes the form and returns actions
3. Playwright executes the actions
4. Claude extracts records from results

Run: python tests/test_ai_scraper.py
"""
import asyncio
import sys
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()


async def run():
    from src.scrapers.ai_scraper import AIScraper

    base_url = "https://armsweb.co.pierce.wa.us/"

    print("=" * 60)
    print("  AI Scraper Test — Pierce County, WA (Probate)")
    print("=" * 60)

    async with AIScraper(
        base_url=base_url,
        county="pierce",
        state="WA",
        record_types=["probate"],
    ) as scraper:
        records = await scraper.scrape("01/01/2026", "03/18/2026")

    print(f"\n{'=' * 60}")
    print(f"  Results: {len(records)} records")
    print(f"  AI Cost: ${scraper.ai_cost:.4f}")
    print(f"  Tokens:  {scraper.ai_tokens}")
    print(f"{'=' * 60}")

    for i, r in enumerate(records[:5]):
        print(f"\n  Record {i+1}:")
        print(f"    Date:    {r.date_recorded}")
        print(f"    Name:    {r.party_name}")
        print(f"    Parcel:  {r.parcel_id}")
        print(f"    Address: {r.property_address}")

    if len(records) > 5:
        print(f"\n  ... and {len(records) - 5} more records")


if __name__ == "__main__":
    asyncio.run(run())
