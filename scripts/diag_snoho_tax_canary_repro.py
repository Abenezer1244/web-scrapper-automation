"""Reproduce the canary failure for snohomish/tax_delinquent.

Mirrors _canary_check_impl exactly (allowlist refresh, 7-day window, same
_canary_scrape helper) but for ONE connector, printing the full traceback.
Target is snohomishcountywa.gov -- NOT King. Unrelated to the King rate-block.
"""
import asyncio
import os
import sys
import traceback
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from src.api.middleware.security import register_connector_domains_from_db  # noqa: E402
from src.db.models import CountyConnector  # noqa: E402
from src.db.session import SyncSessionLocal  # noqa: E402
from src.scrapers.registry import get_scraper_class  # noqa: E402
from src.workers.scheduler_helpers.health import _canary_scrape  # noqa: E402

register_connector_domains_from_db()

with SyncSessionLocal() as db:
    conns = db.execute(select(CountyConnector).where(CountyConnector.active)).scalars().all()

target = [
    c for c in conns
    if c.county.lower() == "snohomish" and "tax_delinquent" in (c.record_types or [])
]
print(f"matched {len(target)} connector(s)")
for c in target:
    print(f"\ncounty={c.county} state={c.state!r} mode={c.scraper_mode} "
          f"health={c.health_status}")
    print(f"base_url={c.base_url}")
    print(f"record_types={c.record_types}  max_date_range_days={c.max_date_range_days}")
    try:
        klass, matched_rt = get_scraper_class(c.county, c.state, c.record_types[0])
        print(f"resolved -> {getattr(klass,'func',klass)} (rt={matched_rt})")
    except Exception:
        print("REGISTRY FAILED:")
        traceback.print_exc()
        continue

    today = datetime.now(UTC).date()
    week_ago = today - timedelta(days=7)
    print(f"probing {week_ago:%m/%d/%Y} .. {today:%m/%d/%Y}")
    try:
        recs = asyncio.run(
            _canary_scrape(klass, week_ago.strftime("%m/%d/%Y"), today.strftime("%m/%d/%Y"))
        )
        print(f"RESULT: {len(recs)} records  -> canary would set "
              f"{'healthy' if recs else 'degraded//historical-probe'}")
        # Non-PII sample only. These records carry owner names and home addresses;
        # this runs under `railway run` against PRODUCTION, so printing the record
        # would put more PII into Railway logs than the real canary ever does
        # (it logs counts and an exception class, never sample rows).
        for r in recs[:3]:
            ed = r.enrichment_data or {}
            print(f"    parcel={r.parcel_id} bill_year={ed.get('bill_year')} "
                  f"amount={ed.get('delinquent_amount')} layout={ed.get('source_layout')}")
    except Exception:
        print("CANARY EXCEPTION -> health_status='down'. Full traceback:")
        traceback.print_exc()
