"""Sprint 2 verification: Spokane County probate via EagleWebScraper.

Runs headless against Spokane's EagleWeb portal for a 30-day window
and prints per-source parcel-extraction counters from the template.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PLAYWRIGHT_HEADLESS", "true")


async def main() -> int:
    from src.scrapers.templates.eagleweb import EagleWebScraper

    date_from = "03/01/2026"
    date_to = "04/01/2026"

    print(f"\n=== Spokane WA probate (Sprint 2 verify): {date_from} to {date_to} ===\n", flush=True)

    async with EagleWebScraper(
        base_url="https://recording.spokanecounty.org/recorder/web/",
        county="Spokane",
        state="WA",
        record_types=["probate"],
    ) as scraper:
        records = await scraper.scrape(date_from, date_to)

    with_pid = sum(1 for r in records if r.parcel_id)
    print(f"\n=== RESULTS ===", flush=True)
    print(f"Total records: {len(records)}", flush=True)
    if records:
        print(f"With parcel_id: {with_pid}/{len(records)} ({100*with_pid/len(records):.0f}%)", flush=True)

    for i, r in enumerate(records[:5], 1):
        print(
            f"  {i}. date={r.date_recorded} party={r.party_name!r} parcel={r.parcel_id}",
            flush=True,
        )

    return 0 if records else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
