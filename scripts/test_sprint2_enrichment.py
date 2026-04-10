"""Sprint 2 enrichment verification: Clark + Spokane property/mailing address.

Scrapes probate records then runs batch GIS enrichment (same code path
the Railway worker uses post-scrape). Reports hit rates so we can
confirm the Sprint 2 gate of 95%+ property/mailing coverage.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PLAYWRIGHT_HEADLESS", "true")


async def scrape_clark():
    from src.scrapers.clark_wa import ClarkWAScraper
    async with ClarkWAScraper(record_type="probate") as s:
        return await s.scrape("03/01/2026", "04/01/2026")


async def scrape_spokane():
    from src.scrapers.templates.eagleweb import EagleWebScraper
    async with EagleWebScraper(
        base_url="https://recording.spokanecounty.org/recorder/web/",
        county="Spokane",
        state="WA",
        record_types=["probate"],
    ) as s:
        return await s.scrape("03/01/2026", "04/01/2026")


def enrich_and_report(county_name: str, records: list) -> None:
    from src.scrapers.enrichment.county_gis import batch_enrich_parcels_gis

    parcel_ids = [r.parcel_id for r in records if r.parcel_id]
    unique_parcels = list(dict.fromkeys(parcel_ids))  # preserve order, dedupe
    print(f"\n=== {county_name}: {len(records)} records, {len(unique_parcels)} unique parcels ===", flush=True)

    if not unique_parcels:
        print("  No parcels to enrich", flush=True)
        return

    enriched = batch_enrich_parcels_gis(unique_parcels, county_name.lower(), "WA")

    # Apply enrichment to records
    for r in records:
        if not r.parcel_id:
            continue
        data = enriched.get(r.parcel_id) or {}
        # Also try dash-stripped variant (Clark parcels don't have dashes,
        # Spokane has dotted format — both already clean)
        if not data:
            data = enriched.get(r.parcel_id.replace("-", "")) or {}
        if data.get("property_address"):
            r.enrichment_data["property_address"] = data["property_address"]
        if data.get("mailing_address"):
            r.enrichment_data["mailing_address"] = data["mailing_address"]

    with_property = sum(1 for r in records if r.enrichment_data.get("property_address"))
    with_mailing = sum(1 for r in records if r.enrichment_data.get("mailing_address"))

    n = len(records)
    print(f"  property_address: {with_property}/{n} ({100*with_property/n:.0f}%)", flush=True)
    print(f"  mailing_address:  {with_mailing}/{n} ({100*with_mailing/n:.0f}%)", flush=True)

    # Show 3 sample enriched records
    print("  Sample enriched records:", flush=True)
    shown = 0
    for r in records:
        if shown >= 3:
            break
        prop = r.enrichment_data.get("property_address")
        if prop:
            print(f"    parcel={r.parcel_id} party={r.party_name[:40]!r}", flush=True)
            print(f"      property: {prop}", flush=True)
            print(f"      mailing:  {r.enrichment_data.get('mailing_address')}", flush=True)
            shown += 1


async def main() -> int:
    print("=== Sprint 2 enrichment verification ===", flush=True)

    print("\n[1/2] Scraping Clark County probate...", flush=True)
    clark_records = await scrape_clark()
    enrich_and_report("Clark", clark_records)

    print("\n[2/2] Scraping Spokane County probate...", flush=True)
    spokane_records = await scrape_spokane()
    enrich_and_report("Spokane", spokane_records)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
