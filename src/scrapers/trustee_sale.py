"""Trustee Sale ("Auction Leads") — record type ``trustee_sale``.

Turns the shared ``nts_notices`` auction-data cache into a deliverable lead list.
Unlike ``pre_foreclosure`` (which comes from the recorder index and gets auction
data *matched* on afterward by ``nts_matcher_task``), a ``trustee_sale`` lead IS a
published Notice of Trustee Sale — so its source is a single, known ``nts_notices``
row. Every lead therefore has a real auction date + amount owing by construction.

Only the three counties with an NTS crawler feeding the cache have data:
Pierce (Tacoma Daily Index), Snohomish (Snoho Tribune), King (Queen Anne News) —
mirrors ``nts_matcher_task.NTS_MATCH_COUNTIES``. A county with no notices yields
zero leads (the SELECT returns nothing), never an error.

DB-backed, not a portal: like ``snohomish_wa_pre_foreclosure`` / ``snohomish_wa_
tax_delinquent`` it overrides the Playwright lifecycle to no-ops and reads the DB
inside ``scrape()``. The worker (``_run_scraper``) only passes ``record_type`` to
the constructor — never ``county`` — so county identity lives on thin per-county
subclasses (the King-alias precedent), one allowlisted module for all three.

The scraper stashes the source notice id + auction fields in
``enrichment_data["nts_source"]``; the deterministic post-persist finalizer
(``trustee_sale_finalize``) reads that to populate ``results.auction_date`` /
``default_amount`` / ``nts_notice_id`` directly (keyed on the known notice id — no
fuzzy matching). See ``tasks/todo.md``.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func, select

from src.db.models import NtsNotice
from src.db.session import system_sync_session
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.scrapers.preforeclosure import strip_vesting_clause
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.trustee_sale")

RECORD_TYPE = "trustee_sale"


def _record_from_notice(notice: NtsNotice) -> ScrapedRecord:
    """Build a trustee_sale ScrapedRecord from one active nts_notices row.

    Auction data is carried in ``enrichment_data["nts_source"]`` (with the source
    notice id) so the finalizer can write the typed Result columns deterministically
    — the scraper never touches ``results`` and ScrapedRecord has no auction fields.
    """
    rec = ScrapedRecord()
    # grantor = the motivated owner; strip the legal vesting boilerplate the same way
    # the pre_foreclosure leads do so the name shows just the owner(s).
    rec.party_name = BridgeScraper.clean(strip_vesting_clause(notice.grantor))
    rec.property_address = notice.property_address
    rec.parcel_id = notice.parcel
    rec.doc_type = "Notice of Trustee Sale"
    # date_recorded feeds the date window + "new" badge (parity with pre_foreclosure):
    # the NOD transmittal date if present, else the (future) auction date.
    _auction_iso = notice.auction_date.isoformat() if notice.auction_date else None
    rec.date_recorded = notice.nod_date or _auction_iso
    rec.enrichment_data = {
        "source": notice.source,
        "county": notice.county,
        "state": notice.state,
        # nts_source is the finalizer's contract: notice_id is REQUIRED (fail-closed).
        "nts_source": {
            "notice_id": notice.id,
            "auction_date": _auction_iso,
            "default_amount": (
                str(notice.principal_owing) if notice.principal_owing is not None else None
            ),
            "ts_number": notice.ts_number,
            "trustee": notice.trustee,
            "beneficiary": notice.beneficiary,
            "auction_time": notice.auction_time,
            "auction_location": notice.auction_location,
            "source": notice.source,
            "source_url": notice.source_url,
        },
    }
    return rec


class _TrusteeSaleScraper(BridgeScraper):
    """Base: read active, future-dated NTS notices for ``COUNTY`` into leads.

    Not registered directly — the county subclasses below carry ``COUNTY`` because
    the worker never passes county to the constructor.
    """

    COUNTY: str = ""  # lowercase county slug, set by subclass

    def __init__(self, record_type: str = RECORD_TYPE) -> None:
        super().__init__()
        if not self.COUNTY:
            # A bare _TrusteeSaleScraper can't know its county — always a wiring bug.
            raise ValueError(
                "TrusteeSaleScraper requires a county subclass (COUNTY unset)"
            )

    @classmethod
    def collection_scope(cls, record_type: str):
        """SHOW descriptor — trustee_sale surfaces published Notice-of-Trustee-Sale only."""
        from src.scrapers.doc_scope import CollectionScope, DocTypeItem

        if record_type != RECORD_TYPE:
            return None
        return CollectionScope(
            kind="document_type",
            items=(DocTypeItem(label="Notice of Trustee Sale", exact=True),),
            note="Published trustee-sale auction notices (county legal newspaper).",
        )

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        # date_from/date_to unused: nts_notices is a current snapshot of UPCOMING
        # sales (a lead's date_recorded is its FUTURE auction date), so a past-looking
        # window would drop exactly the active sales we want — same rationale as
        # snohomish_wa_pre_foreclosure. We select active + future-dated notices; the
        # user-facing window lives at the results/Lists layer.
        del date_from, date_to

        today = date.today()
        with system_sync_session() as db:
            notices = (
                db.execute(
                    select(NtsNotice)
                    .where(
                        func.lower(NtsNotice.county) == self.COUNTY,
                        NtsNotice.is_active.is_(True),
                        NtsNotice.auction_date.isnot(None),
                        NtsNotice.auction_date >= today,
                    )
                    .order_by(NtsNotice.auction_date.asc())
                )
                .scalars()
                .all()
            )

        records = [_record_from_notice(n) for n in notices]
        if not records:
            _logger.warning(
                "trustee_sale %s: 0 active future-dated NTS notices (no auction leads this run)",
                self.COUNTY,
            )
        _logger.info(
            "trustee_sale %s complete — %d active notices → %d auction leads",
            self.COUNTY,
            len(notices),
            len(records),
        )
        if self.on_progress:
            self.on_progress(1, 1, len(records))
        return records

    async def __aenter__(self) -> "_TrusteeSaleScraper":
        return self

    async def __aexit__(self, *args) -> None:
        pass


class PierceWATrusteeSaleScraper(_TrusteeSaleScraper):
    """Pierce County (WA) auction leads — Tacoma Daily Index NTS cache."""

    COUNTY = "pierce"


class SnohomishWATrusteeSaleScraper(_TrusteeSaleScraper):
    """Snohomish County (WA) auction leads — Snohomish County Tribune NTS cache."""

    COUNTY = "snohomish"


class KingWATrusteeSaleScraper(_TrusteeSaleScraper):
    """King County (WA) auction leads — Queen Anne & Magnolia News NTS cache."""

    COUNTY = "king"
