"""Daily county scrape: populates county_records cache.

Called by the beat scheduler. Scrapes each active county for yesterday's
records (or 90-day backfill if the county has no cached data yet).
Inserts into county_records with ON CONFLICT DO NOTHING for dedup.
"""

import asyncio
import hashlib
import uuid as _uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, insert, select, text

from src.config import settings
from src.db.models import CountyConnector, CountyRecord
from src.db.session import SyncSessionLocal
from src.scrapers.registry import get_scraper_class, UnsupportedCountyError
from src.utils.logger import setup_logger
from src.workers.tasks import _run_scraper

_logger = setup_logger("worker.daily_scrape")


def make_record_hash(county: str, state: str, party_name: str, date_recorded: str, legal_description: str) -> str:
    """MD5 hash for dedup. Excludes timestamps so re-scrapes deduplicate."""
    raw = f"{county}|{state}|{party_name or ''}|{date_recorded or ''}|{legal_description or ''}"
    return hashlib.md5(raw.encode()).hexdigest()


def run_daily_scrape_for_county(county: str, state: str) -> int:
    """Scrape a single county's daily records into county_records.

    Returns the number of new records inserted.
    """
    with SyncSessionLocal() as db:
        today = datetime.now(UTC).date()
        existing = db.execute(
            select(func.count()).select_from(CountyRecord).where(
                func.lower(CountyRecord.county) == county.lower(),
                func.upper(CountyRecord.state) == state.upper(),
                CountyRecord.batch_date == today,
            )
        ).scalar_one()

        if existing > 0:
            _logger.info("County %s/%s already scraped today (%d records), skipping", county, state, existing)
            return 0

        lock_key = int(hashlib.md5(f"{county}|{state}".encode()).hexdigest()[:8], 16)
        got_lock = db.execute(text(f"SELECT pg_try_advisory_lock({lock_key})")).scalar()
        if not got_lock:
            _logger.info("County %s/%s locked by another worker, skipping", county, state)
            return 0

        try:
            total_records = db.execute(
                select(func.count()).select_from(CountyRecord).where(
                    func.lower(CountyRecord.county) == county.lower(),
                    func.upper(CountyRecord.state) == state.upper(),
                )
            ).scalar_one()

            if total_records == 0:
                date_from = (today - timedelta(days=90)).strftime("%m/%d/%Y")
                date_to = today.strftime("%m/%d/%Y")
                _logger.info("Backfill %s/%s: %s to %s", county, state, date_from, date_to)
            else:
                yesterday = today - timedelta(days=1)
                date_from = yesterday.strftime("%m/%d/%Y")
                date_to = today.strftime("%m/%d/%Y")
                _logger.info("Daily scrape %s/%s: %s to %s", county, state, date_from, date_to)

            connector = db.execute(
                select(CountyConnector).where(
                    func.lower(CountyConnector.county) == county.lower(),
                    func.upper(CountyConnector.state) == state.upper(),
                    CountyConnector.active,
                )
            ).scalar_one_or_none()

            if not connector:
                _logger.warning("No active connector for %s/%s", county, state)
                return 0

            record_type = connector.record_types[0] if connector.record_types else "probate"
            try:
                scraper_class, _ = get_scraper_class(county, state, record_type)
            except UnsupportedCountyError as exc:
                _logger.warning("Unsupported county %s/%s: %s", county, state, exc)
                return 0

            import redis
            r = redis.from_url(settings.REDIS_URL, **settings.redis_kwargs())
            records = asyncio.run(_run_scraper(scraper_class, date_from, date_to, r, "system_daily"))

            if not records:
                _logger.info("No records found for %s/%s", county, state)
                return 0

            def _trunc(val, max_len):
                return val[:max_len] if val and len(val) > max_len else val

            batch_size = 1000
            inserted = 0
            for i in range(0, len(records), batch_size):
                batch = records[i:i + batch_size]
                rows = []
                for rec in batch:
                    rec_hash = make_record_hash(
                        county, state,
                        rec.party_name, rec.date_recorded, rec.legal_description,
                    )
                    rows.append({
                        "id": str(_uuid.uuid4()),
                        "county": county.lower(),
                        "state": state.upper(),
                        "doc_type": _trunc(getattr(rec, "doc_type", None), 128),
                        "date_recorded": _trunc(rec.date_recorded, 32),
                        "party_name": _trunc(rec.party_name, 512),
                        "heirs": rec.heirs,
                        "legal_description": rec.legal_description,
                        "parcel_id": _trunc(rec.parcel_id, 64),
                        "property_address": _trunc(rec.property_address, 512),
                        "mailing_address": _trunc(rec.mailing_address, 512),
                        "enrichment_data": rec.enrichment_data or {},
                        "record_hash": rec_hash,
                        "batch_date": today,
                    })

                stmt = insert(CountyRecord).values(rows).on_conflict_do_nothing(index_elements=["record_hash"])
                result = db.execute(stmt)
                inserted += result.rowcount
                db.commit()

            _logger.info("Inserted %d new records for %s/%s (out of %d scraped)", inserted, county, state, len(records))
            return inserted

        finally:
            db.execute(text(f"SELECT pg_advisory_unlock({lock_key})"))
