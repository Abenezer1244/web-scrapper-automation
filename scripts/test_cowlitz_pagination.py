"""Direct test of the Laserfiche WebLink pagination fix against live Cowlitz site.

Runs a 7-day scrape and reports:
- total results from page header
- records extracted from each page
- whether pagination walks beyond page 1
- probate / pre_foreclosure match counts
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x/x")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

# Stub the security middleware to skip pulling the FastAPI/DB stack.
import types as _types

_stub = _types.ModuleType("src.api.middleware.security")
_stub.add_scrape_domain = lambda *_a, **_k: None
_stub.validate_scraping_target = lambda *_a, **_k: None
sys.modules["src.api.middleware.security"] = _stub

from src.scrapers.templates.laserfiche_weblink import LaserficheWebLinkScraper


async def run():
    today = datetime.now().date()
    date_from = (today - timedelta(days=7)).strftime("%m/%d/%Y")
    date_to = today.strftime("%m/%d/%Y")

    url = "https://www.cowlitzinfo.net/WLAudPublic/welcome.aspx?dbid=0&repo=CCIMAGES"

    for record_type in ("probate", "pre_foreclosure"):
        print("=" * 60)
        print(f"COWLITZ {record_type.upper()} — {date_from} to {date_to}")
        print("=" * 60)
        async with LaserficheWebLinkScraper(
            base_url=url,
            county="cowlitz",
            state="WA",
            record_types=[record_type],
            record_type=record_type,
            require_parcel_id=False,
        ) as scraper:
            records = await scraper.scrape(date_from, date_to)
            print(f"\nFINAL: {len(records)} {record_type} records")
            for r in records[:5]:
                print(f"  - {r.date_recorded} | {r.doc_type} | {(r.party_name or '')[:40]}")


if __name__ == "__main__":
    asyncio.run(run())
