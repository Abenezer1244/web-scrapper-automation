"""Sprint 2 verification: Kitsap County probate via EagleWebScraper."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PLAYWRIGHT_HEADLESS", "true")


async def main() -> int:
    from src.scrapers.templates.eagleweb import EagleWebScraper
    from src.scrapers.enrichment.county_gis import batch_enrich_parcels_gis

    date_from = "03/01/2026"
    date_to = "04/01/2026"

    print(f"\n=== Kitsap WA probate (Sprint 2 verify): {date_from} to {date_to} ===\n", flush=True)

    async with EagleWebScraper(
        base_url="https://kcwaimg.kitsap.gov/recorder/web/",
        county="Kitsap",
        state="WA",
        record_types=["probate"],
    ) as scraper:
        records = await scraper.scrape(date_from, date_to)

    n = len(records)
    with_pid = sum(1 for r in records if r.parcel_id)
    print("\n=== SCRAPE RESULTS ===", flush=True)
    print(f"Total records: {n}", flush=True)
    if n:
        print(f"With parcel_id: {with_pid}/{n} ({100*with_pid/n:.0f}%)", flush=True)

    if with_pid:
        unique = list(dict.fromkeys(r.parcel_id for r in records if r.parcel_id))
        print("\n=== ENRICHMENT ===", flush=True)
        print(f"Unique parcels: {len(unique)}", flush=True)
        print(f"Sample parcels: {unique[:8]}", flush=True)
        enriched = batch_enrich_parcels_gis(unique, "kitsap", "WA")

        for r in records:
            if not r.parcel_id:
                continue
            d = enriched.get(r.parcel_id) or enriched.get(r.parcel_id.replace("-", "")) or {}
            if d.get("property_address"):
                r.enrichment_data["property_address"] = d["property_address"]
            if d.get("mailing_address"):
                r.enrichment_data["mailing_address"] = d["mailing_address"]

        wp = sum(1 for r in records if r.enrichment_data.get("property_address"))
        wm = sum(1 for r in records if r.enrichment_data.get("mailing_address"))
        print(f"property_address: {wp}/{n} ({100*wp/n:.0f}%)", flush=True)
        print(f"mailing_address:  {wm}/{n} ({100*wm/n:.0f}%)", flush=True)

        unmatched = sorted(set(r.parcel_id for r in records if not r.enrichment_data.get("property_address")))
        if unmatched:
            print(f"\nUnmatched parcels ({len(unmatched)}):", flush=True)
            for p in unmatched:
                print(f"  len={len(p)} {p}", flush=True)

        shown = 0
        for r in records:
            if shown >= 3:
                break
            prop = r.enrichment_data.get("property_address")
            if prop:
                print(f"  {r.date_recorded} | {r.party_name[:40]}", flush=True)
                print(f"    parcel={r.parcel_id} | {prop}", flush=True)
                shown += 1

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
