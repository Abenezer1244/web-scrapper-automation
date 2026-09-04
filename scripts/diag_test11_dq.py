"""Data-quality profile of what the FIXED Pierce pre_foreclosure scrape produces.

    railway run --service worker python scripts/diag_test11_dq.py [from] [to]
"""
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.ERROR)

D_FROM = sys.argv[1] if len(sys.argv) > 1 else "06/04/2026"
D_TO = sys.argv[2] if len(sys.argv) > 2 else "09/01/2026"
OUT = sys.argv[3] if len(sys.argv) > 3 else "test11_dq.json"

FIELDS = [
    "date_recorded", "party_name", "parcel_id", "property_address",
    "mailing_address", "legal_description", "doc_type", "heirs",
    "delinquent_amount", "delinquent_bill_year", "phone", "email",
]


async def main():
    from src.scrapers.pierce_wa_probate import PierceWAPreForeclosureScraper

    scraper = PierceWAPreForeclosureScraper()
    async with scraper:
        recs = await scraper.scrape(D_FROM, D_TO)

    rows = [r.to_dict() for r in recs]
    n = len(rows)
    print(f"TOTAL RECORDS: {n}  (marker said {getattr(scraper, '_record_count', '?')})")
    print(f"{'field':22} {'populated':>10} {'missing':>8} {'fill%':>7}")
    for f in FIELDS:
        pop = sum(1 for r in rows if str(r.get(f) or "").strip())
        print(f"{f:22} {pop:>10} {n - pop:>8} {100.0 * pop / n if n else 0:>6.1f}%")

    # value-shape checks that matter for lead quality
    import re
    bad_parcel = [r.get("parcel_id") for r in rows
                  if r.get("parcel_id") and not re.fullmatch(r"\d{10}", str(r["parcel_id"]))]
    placeholder = [r.get("party_name") for r in rows
                   if str(r.get("party_name") or "").strip().upper()
                   in {"UNKNOWN", "N/A", "NA", "TEST", "PUBLIC", "NONE", "-"}]
    bad_date = [r.get("date_recorded") for r in rows
                if r.get("date_recorded")
                and not re.fullmatch(r"\d{2}/\d{2}/\d{4}", str(r["date_recorded"]))]
    same_addr = sum(1 for r in rows
                    if r.get("property_address") and r.get("mailing_address")
                    and str(r["property_address"]).strip().upper()
                    == str(r["mailing_address"]).strip().upper())
    print(f"\nparcel_id not 10 digits : {len(bad_parcel)} {bad_parcel[:5]}")
    print(f"placeholder party_name  : {len(placeholder)} {placeholder[:5]}")
    print(f"date_recorded malformed : {len(bad_date)} {bad_date[:5]}")
    print(f"mailing == property     : {same_addr}")
    print(f"doc_type mix            : "
          f"{json.dumps({d: sum(1 for r in rows if r.get('doc_type') == d) for d in sorted({str(r.get('doc_type')) for r in rows})})}")

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=1, default=str)
    print(f"\nwrote {OUT} ({n} rows)")
    print("\n--- first 3 rows ---")
    for r in rows[:3]:
        print(json.dumps({k: r.get(k) for k in FIELDS}, default=str))


asyncio.run(main())
