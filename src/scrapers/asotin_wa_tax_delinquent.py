"""Asotin County (WA) — Tax Delinquent leads from the per-parcel PACS tax cache.

Asotin publishes no delinquent roll. workers/pacs_tax_crawler walks every parcel's
PACS tax table into ``pacs_tax_status``; this scraper only reads it (DB-backed, like
trustee_sale), so a job is fast and never talks to the county.

WHAT QUALIFIES (owner decision 2026-11-12, matching King/Snohomish): past-due tax from
a year BEFORE the current one. Current-year halves a few weeks late are not distress
(on 2026-11-12, 6 of 60 random parcels owed only 2026 tax, 12 days after its due
date). ``delinquent_amount`` is everything past due on the parcel today (the real
balance); ``bill_year`` is the oldest year owing, the same meaning the shared
months-delinquent filter gives King's and Snohomish's.

NEVER A FALSE ZERO: the cache must cover at least 95% of the county's inventory with
answers from the last 10 days, or the job fails loudly instead of returning a short
or empty list.

STRICT QUALITY: a qualifying parcel missing its owner, property address or mailing
address is HELD (stays in the cache, counted in the log), never delivered.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from src.db.models import PacsTaxStatus
from src.db.session import system_sync_session
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.scrapers.reliability import ScraperExecutionError
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.asotin_wa_tax_delinquent")

RECORD_TYPE = "tax_delinquent"
SOURCE = "asotin_pacs_tax"  # dedup._TRUSTED_TAX_SOURCES
_COUNTY, _STATE = "asotin", "WA"
_FRESH = timedelta(days=10)
_MIN_COVERAGE = 0.95


def coverage(total: int, fresh: int) -> float:
    return fresh / total if total else 0.0


def record_from_row(row: PacsTaxStatus) -> ScrapedRecord:
    rec = ScrapedRecord()
    rec.party_name = row.owner_name
    rec.parcel_id = row.parcel_id
    rec.property_address = row.property_address
    rec.mailing_address = row.mailing_address
    rec.doc_type = "Delinquent Property Tax"
    rec.date_recorded = f"{row.as_of.month}/{row.as_of.day}/{row.as_of.year}"
    rec.enrichment_data = {
        "source": SOURCE,
        "delinquent_amount": str(row.delinquent_amount),
        "bill_year": row.oldest_delinquent_year,
        "years_owing": row.years_owing,
        "tax_as_of": row.as_of.isoformat(),
    }
    # One lead per parcel per status read: stable across re-runs of the same snapshot.
    rec.raw_html_hash = BridgeScraper.make_hash(
        {"parcel": row.parcel_id, "as_of": row.as_of.isoformat(),
         "amount": str(row.delinquent_amount)})
    return rec


def deliverable(row: PacsTaxStatus) -> bool:
    return bool(row.owner_name and row.property_address and row.mailing_address)


class AsotinWATaxDelinquentScraper(BridgeScraper):
    """Read Asotin's qualifying delinquent parcels out of pacs_tax_status."""

    @classmethod
    def collection_scope(cls, record_type: str):
        from src.scrapers.doc_scope import CollectionScope, DocTypeItem

        if record_type != RECORD_TYPE:
            return None
        return CollectionScope(
            kind="document_type",
            items=(DocTypeItem(label="Delinquent Property Tax", exact=True),),
            note="Parcels owing property tax from a prior year (county assessor tax records).",
        )

    def __init__(self, record_type: str = RECORD_TYPE) -> None:
        super().__init__()

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        # Tax status is a snapshot, not a filing: the date window does not apply.
        self.report_stage("searching")
        since = datetime.now(UTC) - _FRESH
        scope = (PacsTaxStatus.county == _COUNTY, PacsTaxStatus.state == _STATE)
        with system_sync_session() as db:
            total = db.execute(select(func.count()).select_from(PacsTaxStatus)
                               .where(*scope)).scalar_one()
            fresh = db.execute(select(func.count()).select_from(PacsTaxStatus)
                               .where(*scope, PacsTaxStatus.checked_at >= since)).scalar_one()
            rows = db.execute(select(PacsTaxStatus).where(
                *scope,
                PacsTaxStatus.checked_at >= since,
                PacsTaxStatus.outcome == "found",
                PacsTaxStatus.privacy_suppression_id.is_(None),
                PacsTaxStatus.delinquent_amount > 0,
                PacsTaxStatus.oldest_delinquent_year < func.extract("year", PacsTaxStatus.as_of),
            ).order_by(PacsTaxStatus.delinquent_amount.desc())).scalars().all()
        cov = coverage(total, fresh)
        if cov < _MIN_COVERAGE:
            raise ScraperExecutionError(
                _COUNTY, "pacs_tax_cache",
                f"the tax cache covers {fresh} of {total} parcels from the last "
                f"{_FRESH.days} days ({cov:.0%}); refusing to report a partial list",
                record_type=RECORD_TYPE,
            )
        records = [record_from_row(r) for r in rows if deliverable(r)]
        held = len(rows) - len(records)
        _logger.info("asotin tax_delinquent: %d qualifying parcel(s), %d delivered, %d held "
                     "(missing owner/address/mailing); coverage %.1f%%",
                     len(rows), len(records), held, cov * 100)
        if self.on_progress:
            self.on_progress(0, 0, len(records))
        return records

    async def __aenter__(self) -> AsotinWATaxDelinquentScraper:
        return self

    async def __aexit__(self, *args) -> None:
        pass
