"""Sprint 2 verification: Clark County probate via new ClarkWAScraper.

Runs headless against the live Clark portal for a 30-day window and
prints the log lines that reveal pagination + PID extraction health.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PLAYWRIGHT_HEADLESS", "true")


async def main() -> int:
    from src.scrapers.clark_wa import ClarkWAScraper

    date_from = "03/01/2026"
    date_to = "04/01/2026"

    print(f"\n=== Clark WA probate (Sprint 2 verify): {date_from} to {date_to} ===\n", flush=True)

    async with ClarkWAScraper(record_type="probate") as scraper:
        records = await scraper.scrape(date_from, date_to)

    with_pid = sum(1 for r in records if r.parcel_id)
    with_party = sum(1 for r in records if r.party_name)

    print("\n=== RESULTS ===", flush=True)
    print(f"Total records: {len(records)}", flush=True)
    print(f"With parcel_id: {with_pid} ({100*with_pid/len(records):.0f}%)" if records else "With parcel_id: 0", flush=True)
    print(f"With party_name: {with_party}", flush=True)

    for i, r in enumerate(records[:5], 1):
        print(
            f"  {i}. date={r.date_recorded} party={r.party_name!r} "
            f"parcel={r.parcel_id} doc={r.doc_type}",
            flush=True,
        )

    return 0 if records else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
