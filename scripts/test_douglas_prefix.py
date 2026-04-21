"""Test Douglas pre_foreclosure fix: verify party_name is no longer bank/lender."""
import asyncio
import os
import sys
import types
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x/x")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

_stub = types.ModuleType("src.api.middleware.security")
_stub.add_scrape_domain = lambda *_a, **_k: None
_stub.validate_scraping_target = lambda *_a, **_k: None
sys.modules["src.api.middleware.security"] = _stub

from src.scrapers.templates.acclaimweb import AcclaimWebScraper


async def run():
    today = datetime.now().date()
    date_from = (today - timedelta(days=14)).strftime("%m/%d/%Y")
    date_to = today.strftime("%m/%d/%Y")

    url = "https://edocs.douglascountywa.gov/AcclaimWeb"

    for rt in ("pre_foreclosure", "probate"):
        print(f"\n{'='*60}\nDOUGLAS {rt.upper()} — {date_from} to {date_to}\n{'='*60}")
        async with AcclaimWebScraper(
            base_url=url, county="douglas", state="WA",
            record_types=[rt], record_type=rt,
        ) as scraper:
            records = await scraper.scrape(date_from, date_to)
            print(f"\nFINAL: {len(records)} records")
            for r in records[:15]:
                print(f"  {r.date_recorded} | {(r.doc_type or '')[:30]:30} | party='{(r.party_name or '')[:40]}'")


if __name__ == "__main__":
    asyncio.run(run())
