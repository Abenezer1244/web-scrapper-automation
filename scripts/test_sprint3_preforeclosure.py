"""Sprint 3 verification: pre_foreclosure on all 7 Sprint-2 counties.

Runs sequentially against live portals and reports parcel/property/
mailing enrichment per county. Gate: at least 3 counties at 90%+.
"""
import asyncio
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("PLAYWRIGHT_HEADLESS", "true")

DATE_FROM = "03/01/2026"
DATE_TO = "04/01/2026"


async def run_pierce():
    from src.scrapers.pierce_wa_probate import PierceWAARMSScraper
    async with PierceWAARMSScraper(record_type="pre_foreclosure") as s:
        return await s.scrape(DATE_FROM, DATE_TO)


async def run_king():
    from src.scrapers.king_wa_probate import KingCountyLandmarkWebScraper
    async with KingCountyLandmarkWebScraper(record_type="pre_foreclosure") as s:
        return await s.scrape(DATE_FROM, DATE_TO)


async def run_clark():
    from src.scrapers.clark_wa import ClarkWAScraper
    async with ClarkWAScraper(record_type="pre_foreclosure") as s:
        return await s.scrape(DATE_FROM, DATE_TO)


async def run_spokane():
    from src.scrapers.templates.eagleweb import EagleWebScraper
    async with EagleWebScraper(
        base_url="https://recording.spokanecounty.org/recorder/web/",
        county="Spokane", state="WA", record_types=["pre_foreclosure"],
    ) as s:
        return await s.scrape(DATE_FROM, DATE_TO)


async def run_thurston():
    from src.scrapers.templates.eagleweb import EagleWebScraper
    async with EagleWebScraper(
        base_url="https://eagleweb.co.thurston.wa.us/thurstonrecorder/web/",
        county="Thurston", state="WA", record_types=["pre_foreclosure"],
    ) as s:
        return await s.scrape(DATE_FROM, DATE_TO)


async def run_kitsap():
    from src.scrapers.templates.eagleweb import EagleWebScraper
    async with EagleWebScraper(
        base_url="https://kcwaimg.kitsap.gov/recorder/web/",
        county="Kitsap", state="WA", record_types=["pre_foreclosure"],
    ) as s:
        return await s.scrape(DATE_FROM, DATE_TO)


async def run_whatcom():
    from src.scrapers.whatcom_wa import WhatcomWAScraper
    async with WhatcomWAScraper(record_type="pre_foreclosure") as s:
        return await s.scrape(DATE_FROM, DATE_TO)


COUNTIES = [
    ("Pierce", "pierce", run_pierce),
    ("King", "king", run_king),
    ("Clark", "clark", run_clark),
    ("Spokane", "spokane", run_spokane),
    ("Thurston", "thurston", run_thurston),
    ("Kitsap", "kitsap", run_kitsap),
    ("Whatcom", "whatcom", run_whatcom),
]


async def enrich_records(county_slug: str, records: list) -> None:
    """Run the same enrichment pipeline the worker uses in production:
    1. Generic WA statewide / county GIS batch
    2. County-specific fallback (King only for now)
    """
    from src.scrapers.enrichment.county_gis import batch_enrich_parcels_gis

    unique = list(dict.fromkeys(r.parcel_id for r in records if r.parcel_id))
    if not unique:
        return

    gis = batch_enrich_parcels_gis(unique, county_slug, "WA")
    for r in records:
        if not r.parcel_id:
            continue
        d = gis.get(r.parcel_id) or gis.get(r.parcel_id.replace("-", "")) or {}
        if d.get("property_address"):
            r.enrichment_data["property_address"] = d["property_address"]
        if d.get("mailing_address"):
            r.enrichment_data["mailing_address"] = d["mailing_address"]

    # King County: eRealProperty + Tax Bill fallback (matches
    # _run_inline_enrichment in src/workers/tasks.py)
    if county_slug == "king":
        from src.scrapers.enrichment.king_county_assessor import batch_enrich_king_county
        needs = [
            r.parcel_id for r in records
            if r.parcel_id and not r.enrichment_data.get("mailing_address")
        ]
        needs = list(dict.fromkeys(needs))
        if needs:
            king = await batch_enrich_king_county(needs)
            for r in records:
                if not r.parcel_id:
                    continue
                d = king.get(r.parcel_id) or {}
                if d.get("property_address") and not r.enrichment_data.get("property_address"):
                    r.enrichment_data["property_address"] = d["property_address"]
                if d.get("mailing_address"):
                    r.enrichment_data["mailing_address"] = d["mailing_address"]


async def report(county_name: str, county_slug: str, records: list) -> tuple[int, int, int, int]:
    n = len(records)
    with_pid = sum(1 for r in records if r.parcel_id)
    print(f"\n=== {county_name}: scrape={n} parcel_id={with_pid}", flush=True)
    if n == 0:
        return 0, 0, 0, 0

    await enrich_records(county_slug, records)

    wp = sum(1 for r in records if r.enrichment_data.get("property_address"))
    wm = sum(1 for r in records if r.enrichment_data.get("mailing_address"))
    print(f"    property={wp}/{n} ({100*wp/n:.0f}%)  mailing={wm}/{n} ({100*wm/n:.0f}%)", flush=True)

    # Show first 2 enriched samples
    shown = 0
    for r in records:
        if shown >= 2:
            break
        prop = r.enrichment_data.get("property_address")
        if prop:
            print(f"    - {r.date_recorded or ''} | {(r.party_name or '')[:35]} | {(r.doc_type or '')[:40]}", flush=True)
            print(f"      {r.parcel_id} | {prop}", flush=True)
            shown += 1
    return n, with_pid, wp, wm


async def main() -> int:
    print(f"=== Sprint 3: pre_foreclosure verification ({DATE_FROM} to {DATE_TO}) ===", flush=True)
    results = []
    for name, slug, fn in COUNTIES:
        print(f"\n[{name}] running...", flush=True)
        try:
            records = await fn()
            n, pid, wp, wm = await report(name, slug, records)
            results.append((name, n, pid, wp, wm, None))
        except Exception as exc:
            tb = traceback.format_exc()
            print(f"[{name}] FAILED: {exc}", flush=True)
            print(tb[-600:], flush=True)
            results.append((name, 0, 0, 0, 0, str(exc)[:80]))

    print("\n\n=== SPRINT 3 SCORECARD ===", flush=True)
    print(f"{'County':<10} {'N':>5} {'Parcel':>8} {'Prop%':>7} {'Mail%':>7}  Status", flush=True)
    gate_pass = 0
    for name, n, pid, wp, wm, err in results:
        if err:
            print(f"{name:<10} {'ERR':>5}                           {err}", flush=True)
            continue
        prop_pct = (100*wp/n) if n else 0
        mail_pct = (100*wm/n) if n else 0
        status = "PASS" if n >= 5 and prop_pct >= 90 else ("LOW-VOL" if n < 5 and prop_pct >= 90 else "FAIL")
        if status == "PASS":
            gate_pass += 1
        print(f"{name:<10} {n:>5} {pid:>8} {prop_pct:>6.0f}% {mail_pct:>6.0f}%  {status}", flush=True)

    print(f"\nGate: {gate_pass}/7 counties pass at >=5 records AND >=90% property (need 3 to ship Sprint 3)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
