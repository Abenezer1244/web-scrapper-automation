"""Live smoke test for the Snohomish pre_foreclosure NTS scraper (no DB writes).

Instantiates SnohomishWAPreForeclosureScraper directly (no connector/registry needed)
and runs scrape() against the CURRENT Snohomish County Tribune Legals PDF, printing a
summary of the leads it yields. Proves the PDF -> leads path end-to-end before we
register the connector.

Run in prod env for parity with the worker:
    railway run --service worker python scripts/test_snoho_preforeclosure.py
"""
import asyncio
import sys

sys.path.insert(0, ".")  # railway-run executes from repo root; make `src` importable


async def _main() -> int:
    from src.scrapers.snohomish_wa_pre_foreclosure import (
        SnohomishWAPreForeclosureScraper,
    )

    async with SnohomishWAPreForeclosureScraper(record_type="pre_foreclosure") as s:
        records = await s.scrape("2000-01-01", "2100-01-01")

    print(f"\n=== Snohomish pre_foreclosure: {len(records)} lead(s) ===")
    future = 0
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date()
    for i, r in enumerate(records, 1):
        ed = r.enrichment_data or {}
        ad = ed.get("auction_date")
        is_future = False
        if ad:
            try:
                mm, dd, yy = (int(x) for x in ad.split("/"))
                is_future = datetime(yy, mm, dd).date() >= today
            except (ValueError, AttributeError):
                is_future = False
        future += int(is_future)
        print(
            f"[{i}] grantor={r.party_name!r}\n"
            f"     addr={r.property_address!r} parcel={r.parcel_id!r}\n"
            f"     date_recorded={r.date_recorded!r} doc_type={r.doc_type!r}\n"
            f"     ts#={ed.get('ts_number')!r} auction={ad!r} "
            f"({'FUTURE' if is_future else 'past/none'}) "
            f"default={ed.get('default_amount')!r} trustee={ed.get('trustee')!r}"
        )

    print(
        f"\nSummary: {len(records)} leads, {future} with a FUTURE-dated auction "
        f"(matcher can only write Result.auction_date for future-dated notices)."
    )
    # Non-zero exit if nothing parsed, so the harness surfaces a dead source loudly.
    return 0 if records else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
