"""Dump all unique doc types from Cowlitz's 7-day window.

Used to decide which record_type configs should be wired up for Cowlitz.
"""
import asyncio
import os
import sys
import types
from collections import Counter
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x/x")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

_stub = types.ModuleType("src.api.middleware.security")
_stub.add_scrape_domain = lambda *_a, **_k: None
_stub.validate_scraping_target = lambda *_a, **_k: None
sys.modules["src.api.middleware.security"] = _stub

from src.scrapers.templates.laserfiche_weblink import LaserficheWebLinkScraper


async def run():
    today = datetime.now().date()
    date_from = (today - timedelta(days=7)).strftime("%m/%d/%Y")
    date_to = today.strftime("%m/%d/%Y")
    url = "https://www.cowlitzinfo.net/WLAudPublic/welcome.aspx?dbid=0&repo=CCIMAGES"

    # Pass record_type=None so no filter is applied — we want every doc type
    async with LaserficheWebLinkScraper(
        base_url=url,
        county="cowlitz",
        state="WA",
        record_types=[],
        record_type=None,
        require_parcel_id=False,
    ) as scraper:
        records = await scraper.scrape(date_from, date_to)
        types_counter = Counter((r.doc_type or "UNKNOWN") for r in records)
        print(f"\n\n{'='*60}")
        print(f"COWLITZ DOC TYPES — {date_from} to {date_to} ({len(records)} records)")
        print(f"{'='*60}")
        for dt, count in types_counter.most_common():
            print(f"  {count:4}  {dt}")


if __name__ == "__main__":
    asyncio.run(run())
