"""Asotin County (WA) — Probate leads from published notices to creditors.

Asotin's recorder sits behind a reCAPTCHA, so probate comes from the Lewiston Tribune
notices to creditors instead. The nwmarket crawl (workers/nts_crawler) stores each
Asotin notice in ``probate_notices`` together with the parcels tied to the estate
(enrichment/asotin_parcels, owner rule "name + family tie", 2026-11-14). This scraper
only reads that table (DB-backed, like trustee_sale), so a job never talks to the county.

ONE LEAD PER TIED PARCEL. The party is the parcel's owner of record in "LAST, FIRST"
form (the decedent as the county lists them). The personal representatives only decided
the family tie and are never delivered, not even as heirs.

STRICT QUALITY: a parcel missing its parcel number, owner, property address or mailing
address is HELD (counted in the log), never delivered. Notices with no tied parcel were
held at crawl time and never reach this scraper.

NEVER A FALSE ZERO: an empty list fails the job instead when the crawl has stopped
reading the newspaper (the same heartbeat as Asotin's auction leads), when a notice in
the window is still waiting on the county layer ("failed", re-checked by the crawl), or
when the window is older than the notices kept (probate_ingest.RETENTION_DAYS).

The property address is the street alone, never with the optional city: the billing
dedup key is parcel|address, so an address that grew a city between crawls would bill
the same parcel twice (Codex). ZIP and state go in their own fields.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select

from src.db.models import ProbateNoticeRow
from src.db.session import system_sync_session
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.scrapers.enrichment.source_health import get_source_state
from src.scrapers.reliability import ScraperExecutionError
from src.scrapers.trustee_sale import _parse_mdy, cache_staleness_reason, nts_crawl_heartbeat_key
from src.utils.lead_signals import auction_reference_date
from src.utils.logger import setup_logger
from src.workers.probate_ingest import RETENTION_DAYS

_logger = setup_logger("scraper.asotin_wa_probate")

RECORD_TYPE = "probate"
DOC_TYPE = "Probate Notice to Creditors"
_COUNTY, _STATE = "asotin", "WA"
_REQUIRED = ("parcel_id", "party_name", "property_address", "mailing_address")


def deliverable(parcel: dict) -> bool:
    return all(isinstance(parcel.get(k), str) and parcel[k].strip() for k in _REQUIRED)


def record_from_parcel(notice: ProbateNoticeRow, parcel: dict) -> ScrapedRecord:
    rec = ScrapedRecord()
    rec.party_name = parcel["party_name"]
    rec.parcel_id = parcel["parcel_id"]
    rec.property_address = parcel["property_address"].strip()
    rec.property_zip = (parcel.get("property_zip") or "").strip() or None
    rec.property_state = _STATE
    rec.mailing_address = parcel["mailing_address"]
    rec.doc_type = DOC_TYPE
    d = notice.first_published
    rec.date_recorded = f"{d.month}/{d.day}/{d.year}"
    rec.enrichment_data = {
        "source": notice.source,
        "cause_no": notice.cause_no,
        "first_published": d.isoformat(),
        "parcel_ties": list(parcel.get("ties") or []),
        # The estate was tied to this parcel by name plus a family tie, not by a
        # recorded deed: say so on the lead.
        "matched_by_name": True,
    }
    # One lead per (cause, parcel): stable across re-runs and re-reads of the notice.
    rec.raw_html_hash = hashlib.sha256(
        f"probate|{notice.source}|{notice.cause_no}|{rec.parcel_id}".encode()).hexdigest()[:32]
    return rec


class AsotinWAProbateScraper(BridgeScraper):
    """Read Asotin's estate parcels out of probate_notices."""

    @classmethod
    def collection_scope(cls, record_type: str):
        from src.scrapers.doc_scope import CollectionScope, DocTypeItem

        if record_type != RECORD_TYPE:
            return None
        return CollectionScope(
            kind="document_type",
            items=(DocTypeItem(label=DOC_TYPE, exact=True),),
            note="Published probate notices to creditors (county legal newspaper), "
                 "matched to the estate's parcels.",
        )

    def __init__(self, record_type: str = RECORD_TYPE) -> None:
        super().__init__()

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        self.report_stage("searching")
        start, end = _parse_mdy(date_from), _parse_mdy(date_to)
        if start is None or end is None or end < start:
            # A filing window is required; guessing one could silently drop leads.
            raise ScraperExecutionError(
                _COUNTY, "probate_notices",
                f"unusable date window {date_from!r}..{date_to!r}", record_type=RECORD_TYPE)
        kept_from = auction_reference_date() - timedelta(days=RETENTION_DAYS)
        if end < kept_from:
            raise ScraperExecutionError(
                _COUNTY, "probate_notices",
                f"the window ends {end}, before the oldest notice kept ({kept_from}); "
                "older notices are deleted after retention", record_type=RECORD_TYPE)
        if start < kept_from:
            _logger.warning("asotin probate: window starts %s, before the oldest notice "
                            "kept (%s); only %s..%s is covered", start, kept_from,
                            kept_from, end)
        records = self._read(start, end)
        if self.on_progress:
            self.on_progress(0, 0, len(records))
        return records

    def _read(self, start: date, end: date) -> list[ScrapedRecord]:
        scope = (ProbateNoticeRow.county == _COUNTY, ProbateNoticeRow.state == _STATE)
        with system_sync_session() as db:
            notices = db.execute(select(ProbateNoticeRow).where(
                *scope,
                ProbateNoticeRow.match_outcome == "matched",
                ProbateNoticeRow.privacy_suppression_id.is_(None),
                ProbateNoticeRow.first_published >= start,
                ProbateNoticeRow.first_published <= end,
            ).order_by(ProbateNoticeRow.first_published, ProbateNoticeRow.cause_no)).scalars().all()
            # Notices the county layer has not answered yet: an unknown, not a zero.
            pending = db.execute(select(func.count()).select_from(ProbateNoticeRow).where(
                *scope,
                ProbateNoticeRow.match_outcome == "failed",
                ProbateNoticeRow.privacy_suppression_id.is_(None),
                ProbateNoticeRow.first_published >= start,
                ProbateNoticeRow.first_published <= end,
            )).scalar_one()
            heartbeat = get_source_state(db, nts_crawl_heartbeat_key(_COUNTY))
            last_fetched_at = db.execute(
                select(func.max(ProbateNoticeRow.fetched_at)).where(*scope)).scalar_one()
        records, held = [], 0
        for notice in notices:
            for parcel in notice.parcels if isinstance(notice.parcels, list) else []:
                if isinstance(parcel, dict) and deliverable(parcel):
                    records.append(record_from_parcel(notice, parcel))
                else:
                    held += 1
        stale = cache_staleness_reason(
            (heartbeat or {}).get("last_success_at"), last_fetched_at, datetime.now(UTC))
        if stale and not records:
            raise ScraperExecutionError(
                _COUNTY, "probate_notices",
                f"0 probate leads, and {stale}. Refusing to report an empty list that may "
                "only mean the crawler is failing", record_type=RECORD_TYPE)
        if pending and not records:
            raise ScraperExecutionError(
                _COUNTY, "probate_notices",
                f"0 probate leads, but {pending} notice(s) in the window are still waiting on "
                "the county parcel layer; refusing to report an empty list",
                record_type=RECORD_TYPE)
        if pending:
            _logger.warning("asotin probate: %d notice(s) in the window still waiting on the "
                            "county parcel layer; this list may be short", pending)
        if stale:
            _logger.warning("asotin probate: delivering %d lead(s) from a stale cache: %s",
                            len(records), stale)
        _logger.info("asotin probate: %s..%s -> %d matched notice(s), %d lead(s), %d parcel(s) "
                     "held (missing parcel/owner/address/mailing)",
                     start, end, len(notices), len(records), held)
        return records

    async def __aenter__(self) -> AsotinWAProbateScraper:
        return self

    async def __aexit__(self, *args) -> None:
        pass
