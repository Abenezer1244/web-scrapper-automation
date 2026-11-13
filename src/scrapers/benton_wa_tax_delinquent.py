"""Benton County (WA) -- tax-delinquent parcels in tax foreclosure.

Source 1: the Treasurer's "Foreclosure and Distraint" page publishes the current
real-property tax-foreclosure listing as a PDF under "Current Listings":

    BENTON COUNTY TAX FORECLOSURE REPORT
    3 PARCELS IN TAX FORECLOSURE - AMOUNTS SHOWN VALID THROUGH 11/13/2026
    SALE IS SCHEDULED ON WWW.GOVEASE.COM AT 8:00 AM(PST), 11/16/2026 and 11/19/2026
    Last Updated: Thursday, October 01, 2026, 8:00 AM
    PARCEL NO. TAXPAYER SITUS ADDRESS LEGAL DESCRIPTION MINIMUM BID
    102892110004002 NELSON SHIRLEY L 2405 W KENNEWICK AVE ... $11,292.00

Source 2: each listed parcel's page on the county's PACS property search (the
same guarded lookup the mailing recovery uses) gives the CURRENT account: one
"Pay Tax Due" row per unpaid year (tax, assessment, penalty, interest, total),
the "as of" date, the owner and the mailing address.

Why both: the listing says the parcel is in tax foreclosure (RCW 84.64: three
years delinquent) and gives the sale; only the live account says whether it is
STILL unpaid. A listing parcel whose account shows nothing due was paid or
redeemed and is not a lead. The owner is verified, not inferred: the PACS page
for exactly this parcel must name the same taxpayer the Treasurer printed.

Real property only. Personal-property distraint (mobile homes, business
property) is a different process on the same page and is never read here.

Not a full delinquency roll: Benton publishes no list of recently delinquent
parcels, only those that reached foreclosure. The source is therefore exempt
from the 18-month tax cap (owner decision 2026-11-12), and every row carries
its real oldest unpaid year.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote, urljoin

from src.api.middleware.security import add_scrape_domain
from src.config import settings
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.scrapers.enrichment import pacs_parcel, pacs_tax
from src.scrapers.sources.nts_pdf import extract_pdf_text
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get

_logger = setup_logger("scraper.benton_wa_tax_delinquent")

SOURCE = "benton_county_tax_foreclosure"
_HOST = "www.bentoncountywa.gov"
_PAGE_URL = f"https://{_HOST}/government/elected_officials/treasurer/foreclosure_and_distraint.php"
_HEADERS = {"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"}
_MAX_PDF_BYTES = 5_000_000

add_scrape_domain(_HOST)

# The listing lives in the "Current Listings" folder; the signed notice of sale sits
# beside it and is not a parcel list.
_LISTING_HREF_RE = re.compile(r'href="([^"]*Current%20Listings/[^"]*\.pdf[^"]*|[^"]*Current Listings/[^"]*\.pdf[^"]*)"', re.I)
_BASE_HREF_RE = re.compile(r'<base\s+href="([^"]+)"', re.I)
_PARCEL_ROW_RE = re.compile(r"^(\d{15})\s+(.*?)\s+\$([\d,]+\.\d{2})\s*$")
_COUNT_RE = re.compile(r"(\d+)\s+PARCELS?\s+IN\s+TAX\s+FORECLOSURE", re.I)
_VALID_THROUGH_RE = re.compile(r"VALID\s+THROUGH\s+(\d{1,2}/\d{1,2}/\d{4})", re.I)
_SALE_LINE_RE = re.compile(r"SALE\s+IS\s+SCHEDULED\s+ON\s+(\S+)\s+AT\s+([^,]+),\s*(.+)", re.I)
_DATE_RE = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")
_UPDATED_RE = re.compile(r"Last\s+Updated:\s*\w+,\s*(\w+\s+\d{1,2},\s*\d{4})", re.I)
_TOTAL_RE = re.compile(r"TOTAL:\s*\$([\d,]+\.\d{2})", re.I)



@dataclass
class ListingParcel:
    parcel: str
    line: str           # everything the Treasurer printed between parcel and bid
    minimum_bid: Decimal


@dataclass
class Listing:
    parcels: list[ListingParcel]
    stated_count: int
    valid_through: str | None
    sale_venue: str | None
    sale_time: str | None
    sale_dates: list[str]
    last_updated: str | None
    total: Decimal | None


def _money(text: str) -> Decimal:
    return Decimal(text.replace("$", "").replace(",", "").strip())


def parse_listing(text: str) -> Listing:
    """The Treasurer's foreclosure report text -> its parcels and header facts.

    Raises ValueError when the report does not reconcile with itself (row count vs
    the stated count, or the rows' bids vs the stated total): a partial parse would
    silently drop a foreclosure.
    """
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    joined = " ".join(lines)
    count_m = _COUNT_RE.search(joined)
    if count_m is None:
        raise ValueError("Benton foreclosure report: no 'N PARCELS IN TAX FORECLOSURE' header")
    parcels = [ListingParcel(m.group(1), m.group(2), _money(m.group(3)))
               for m in (_PARCEL_ROW_RE.match(ln) for ln in lines) if m]
    stated = int(count_m.group(1))
    if len(parcels) != stated:
        raise ValueError(f"Benton foreclosure report lists {stated} parcels but {len(parcels)} rows parsed")
    total_m = _TOTAL_RE.search(joined)
    total = _money(total_m.group(1)) if total_m else None
    if total is not None and sum(p.minimum_bid for p in parcels) != total:
        raise ValueError("Benton foreclosure report: row bids do not add up to the stated TOTAL")
    sale = next((m for m in (_SALE_LINE_RE.search(ln) for ln in lines) if m), None)
    valid = _VALID_THROUGH_RE.search(joined)
    updated = _UPDATED_RE.search(joined)
    return Listing(
        parcels=parcels,
        stated_count=stated,
        valid_through=valid.group(1) if valid else None,
        sale_venue=sale.group(1).lower() if sale else None,
        sale_time=sale.group(2).strip() if sale else None,
        sale_dates=_DATE_RE.findall(sale.group(3)) if sale else [],
        last_updated=updated.group(1) if updated else None,
        total=total,
    )


def find_listing_url(page_html: str, page_url: str = _PAGE_URL) -> str:
    """The current foreclosure listing PDF. The signed notice of sale is not a listing.

    Raises when the page names none: a renamed folder or changed link markup looks
    exactly like "no listing", so absence is never taken as a verified zero.

    Relative links resolve against the page's <base href> (the site root), as a
    browser does, not against the page's own path."""
    base_m = _BASE_HREF_RE.search(page_html)
    base = urljoin(page_url, base_m.group(1)) if base_m else page_url
    for href in _LISTING_HREF_RE.findall(page_html):
        if "notice of sale" in href.replace("%20", " ").lower():
            continue
        return urljoin(base, quote(href, safe="/:?=&%."))
    raise RuntimeError("Benton Treasurer page links no current foreclosure listing PDF; "
                       "cannot tell an empty cycle from a changed page")


def _owner_matches(listing_line: str, owner: str | None) -> str | None:
    """The PACS owner if the Treasurer printed it for this parcel, else None.

    The listing prints TAXPAYER then situs and legal with no delimiter, so the
    owner is verified by prefix: the line must START with exactly the owner name
    the county's own parcel page shows."""
    name = (owner or "").upper().strip()
    line = listing_line.upper()
    if name and (line == name or line.startswith(name + " ")):
        return owner
    return None


def build_records(listing: Listing, answers: dict[str, pacs_tax.TaxAnswer], listing_url: str
                  ) -> tuple[list[ScrapedRecord], dict[str, int]]:
    """Join the listing with the live accounts (pacs_tax answers). Returns
    (records, skip counts). Past due follows pacs_tax (RCW 84.56.020: owing after
    the first-half due date, the current year included), as for every PACS county."""
    records: list[ScrapedRecord] = []
    skipped = {"paid_or_redeemed": 0, "owner_not_verified": 0, "unresolved": 0}
    for item in listing.parcels:
        answer = answers.get(item.parcel)
        # A listed foreclosure parcel always has statements (a paid one shows them at
        # $0.00); none at all, or no owner block, is an unreadable page, never "paid"
        # or "someone else" (Codex).
        if (answer is None or answer.outcome != pacs_tax.FOUND or answer.as_of is None
                or not answer.statements or not answer.owner_name):
            skipped["unresolved"] += 1
            continue
        past_due = answer.past_due
        if not past_due:
            skipped["paid_or_redeemed"] += 1
            continue
        owner = _owner_matches(item.line, answer.owner_name)
        if owner is None:
            skipped["owner_not_verified"] += 1
            continue
        years = sorted({s.year for s in past_due})
        rec = ScrapedRecord()
        rec.parcel_id = item.parcel
        rec.party_name = BridgeScraper.clean(owner)
        rec.property_address = answer.property_address
        rec.mailing_address = answer.mailing_address
        rec.date_recorded = None  # a tax account has years, not a filing date
        rec.enrichment_data = {
            "source": SOURCE,
            "delinquent_amount": str(answer.delinquent_amount.quantize(Decimal("0.01"))),
            "bill_year": answer.oldest_delinquent_year,
            "delinquent_years": years,
            "delinquent_year_count": len(years),
            "oldest_tax_year": answer.oldest_delinquent_year,
            "as_of_date": answer.as_of.strftime("%m/%d/%Y"),
            "account_number": item.parcel,
            "county": "benton",
            "state": "WA",
            "owner_verified": True,
            "foreclosure_status": "tax_foreclosure_sale_scheduled",
            "foreclosure_minimum_bid": str(item.minimum_bid),
            "foreclosure_sale_dates": listing.sale_dates,
            "foreclosure_sale_time": listing.sale_time,
            "foreclosure_sale_venue": listing.sale_venue,
            "foreclosure_listing_valid_through": listing.valid_through,
            "foreclosure_listing_updated": listing.last_updated,
            "foreclosure_listing_url": listing_url,
        }
        records.append(rec)
    return records, skipped


class BentonWATaxDelinquentScraper(BridgeScraper):
    """Benton tax-foreclosure parcels that are still unpaid. Pure HTTP."""

    @classmethod
    def collection_scope(cls, record_type: str):
        from src.scrapers.doc_scope import dataset

        if record_type != "tax_delinquent":
            return None
        return dataset(
            "Collected from the Benton County Treasurer's real-property tax-foreclosure "
            "listing, checked against each parcel's current tax account."
        )

    def __init__(self, record_type: str = "tax_delinquent") -> None:
        super().__init__()

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        # A current snapshot: the listing and the accounts have no date range.
        del date_from, date_to
        self.report_stage("searching")
        page = safe_get(_PAGE_URL, require_allowlisted=True, headers=_HEADERS,
                        timeout=settings.DEFAULT_TIMEOUT)
        page.raise_for_status()
        if "Foreclosure" not in page.text:
            raise RuntimeError("Benton Treasurer foreclosure page did not load as expected")
        listing_url = find_listing_url(page.text)
        pdf = safe_get(listing_url, require_allowlisted=True, headers=_HEADERS,
                       timeout=settings.DEFAULT_TIMEOUT, max_bytes=_MAX_PDF_BYTES)
        pdf.raise_for_status()
        listing = parse_listing(extract_pdf_text(pdf.content))
        if not listing.parcels:
            return []
        answers = await asyncio.to_thread(
            pacs_parcel.resolve_tax, "benton", [p.parcel for p in listing.parcels])
        records, skipped = build_records(listing, answers, listing_url)
        if skipped["unresolved"]:
            # A parcel whose account could not be read is not "not delinquent":
            # fail so the run retries rather than reporting a short list as complete.
            raise RuntimeError(
                f"Benton tax foreclosure: {skipped['unresolved']} of {len(listing.parcels)} "
                "parcel accounts could not be read; results incomplete"
            )
        _logger.info(
            "Benton tax foreclosure: %d listed, %d still delinquent, skipped %s (as of %s)",
            len(listing.parcels), len(records), skipped, datetime.now(UTC).date(),
        )
        if self.on_progress:
            self.on_progress(0, 0, len(records))
        return records

    async def __aenter__(self) -> BentonWATaxDelinquentScraper:
        return self

    async def __aexit__(self, *args) -> None:
        pass


