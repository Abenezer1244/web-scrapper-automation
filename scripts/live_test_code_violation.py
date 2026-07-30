"""Live test harness for all code_violation scrapers (King/Seattle, Pierce/Tacoma).

Pure-HTTP scrapers — no DB/Redis needed for the scrape itself, but importing
`src.config.settings` requires a handful of env vars. We set safe dummies for
the ones the scrape path never touches (DB/Redis), then hit the REAL public
APIs over a recent date window and report record counts, sample rows, and
data-quality red flags.

Usage:
    python scripts/live_test_code_violation.py [days_back]
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta

# Minimal env so `src.config.settings` imports. None of these are used by the
# pure-HTTP scrape path; they exist only to satisfy required pydantic fields.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("DATABASE_URL_SYNC", "postgresql+psycopg2://u:p@localhost/db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SECRET_KEY", "x" * 48)

from src.scrapers.base_scraper import ScrapedRecord
from src.scrapers.king_wa_code_violation import KingWACodeViolationScraper
from src.scrapers.pierce_wa_code_violation import PierceWACodeViolationScraper


def _red_flags(records: list[ScrapedRecord]) -> dict:
    """Count data-quality issues across a record set."""
    flags = {
        "no_date": 0,
        "no_address": 0,
        "no_party": 0,
        "no_parcel": 0,
        "dup_legal": 0,
    }
    seen_legal = set()
    for r in records:
        if not r.date_recorded:
            flags["no_date"] += 1
        if not r.property_address:
            flags["no_address"] += 1
        if not r.party_name:
            flags["no_party"] += 1
        if not r.parcel_id:
            flags["no_parcel"] += 1
        if r.legal_description:
            if r.legal_description in seen_legal:
                flags["dup_legal"] += 1
            seen_legal.add(r.legal_description)
    return flags


async def _run_one(name: str, scraper, date_from: str, date_to: str) -> None:
    print(f"\n{'=' * 70}\n{name}  ({date_from} → {date_to})\n{'=' * 70}")
    try:
        async with scraper as s:
            records = await s.scrape(date_from, date_to)
    except Exception as exc:  # noqa: BLE001 — harness must show whatever happens
        print(f"  RAISED: {type(exc).__name__}: {exc}")
        return

    print(f"  records: {len(records)}")
    if records:
        flags = _red_flags(records)
        print(f"  red_flags: {flags}")
        print("  sample (first 3):")
        for r in records[:3]:
            print(
                f"    - date={r.date_recorded!r} parcel={r.parcel_id!r} "
                f"addr={r.property_address!r}"
            )
            print(f"      party={r.party_name!r} legal={r.legal_description!r}")
    else:
        print("  (zero records — sparse window or a problem)")


async def main() -> None:
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    today = datetime.now()
    date_to = today.strftime("%m/%d/%Y")
    date_from = (today - timedelta(days=days_back)).strftime("%m/%d/%Y")

    await _run_one(
        "KING / Seattle SDCI (Socrata)",
        KingWACodeViolationScraper(),
        date_from,
        date_to,
    )
    await _run_one(
        "PIERCE / Tacoma (ArcGIS)",
        PierceWACodeViolationScraper(),
        date_from,
        date_to,
    )


if __name__ == "__main__":
    asyncio.run(main())
