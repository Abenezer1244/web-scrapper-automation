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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from src.api.middleware.security import add_scrape_domain
from src.config import settings
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.scrapers.enrichment import pacs_parcel
from src.scrapers.enrichment.pacs import compose_pacs_mailing, parse_pacs_detail_html
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

_YEAR_ROW_RE = re.compile(r"^(\d{4})\s*-\s*\d+")
_AS_OF_RE = re.compile(r"Property\s+Tax\s+Information\s+as\s+of\s+(\d{1,2}/\d{1,2}/\d{4})", re.I)


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


@dataclass
class TaxAnswer:
    """What one PACS page says about one parcel's account."""
    outcome: str
    as_of: str | None = None
    years: dict[int, Decimal] = field(default_factory=dict)   # year -> total due
    owners: list[str] = field(default_factory=list)
    mailing: str | None = None
    situs: str | None = None


def parse_tax_detail(html: str, parcel_key: str) -> TaxAnswer:
    """A PACS Property.aspx page -> the account's unpaid years, owner and mailing.

    Same identity rule as the mailing lookup: the page must name exactly this parcel.
    """
    page = parse_pacs_detail_html(html)
    if not page["geo_ids"]:
        return TaxAnswer(pacs_parcel.UNPARSED)
    if len(page["geo_ids"]) != 1 or pacs_parcel.normalize_pacs_parcel(page["geo_ids"][0]) != parcel_key:
        return TaxAnswer(pacs_parcel.PARCEL_MISMATCH)
    soup = BeautifulSoup(html, "html.parser")
    as_of = _AS_OF_RE.search(soup.get_text(" ", strip=True))
    table = soup.find("table", id=re.compile(r"PaymentTaxDue_paymentTable$"))
    years: dict[int, Decimal] = {}
    if table is not None:
        if not table.find_all("tr", class_="dataRow"):
            return TaxAnswer(pacs_parcel.UNPARSED)  # a balance table with no rows we can read
        for row in table.find_all("tr", class_="dataRow"):
            cells = row.find_all("td")
            label = cells[0].get_text(" ", strip=True) if cells else ""
            ym = _YEAR_ROW_RE.match(label)
            total = row.find("td", class_="total")
            if not ym or total is None:
                return TaxAnswer(pacs_parcel.UNPARSED)
            year = int(ym.group(1))
            years[year] = years.get(year, Decimal("0")) + _money(total.get_text(strip=True))
    elif "No Amount Due" not in soup.get_text(" ", strip=True):
        return TaxAnswer(pacs_parcel.UNPARSED)  # neither a balance table nor "nothing due"
    owners = []
    for td in soup.find_all("td"):
        if td.get_text(" ", strip=True).rstrip(":").strip().lower() == "name":
            value = td.find_next_sibling("td")
            if value is not None and value.get_text(" ", strip=True):
                owners.append(re.sub(r"\s+", " ", value.get_text(" ", strip=True)))
    if not owners:
        return TaxAnswer(pacs_parcel.UNPARSED)  # no owner block: unreadable, not a mismatch
    composed = {compose_pacs_mailing(lines) for lines in page["mailing"] if lines}
    return TaxAnswer(
        pacs_parcel.FOUND,
        as_of=as_of.group(1) if as_of else None,
        years={y: v for y, v in years.items() if v > 0},
        owners=owners,
        mailing=composed.pop() if len(composed) == 1 and None not in composed else None,
        situs=" ".join(page["situs"]) if page["situs"] else None,
    )


def _owner_matches(listing_line: str, owners: list[str]) -> str | None:
    """The PACS owner the Treasurer printed for this parcel, or None.

    The listing prints TAXPAYER then situs and legal with no delimiter, so the
    owner is verified by prefix: the line must START with exactly the owner name
    the county's own parcel page shows."""
    line = listing_line.upper()
    for owner in owners:
        name = owner.upper().strip()
        if name and (line == name or line.startswith(name + " ")):
            return owner
    return None


def build_records(listing: Listing, answers: dict[str, TaxAnswer], listing_url: str
                  ) -> tuple[list[ScrapedRecord], dict[str, int]]:
    """Join the listing with the live accounts. Returns (records, skip counts)."""
    records: list[ScrapedRecord] = []
    skipped = {"paid_or_redeemed": 0, "owner_not_verified": 0, "unresolved": 0}
    for item in listing.parcels:
        answer = answers.get(item.parcel)
        if answer is None or answer.outcome != pacs_parcel.FOUND or not answer.as_of:
            skipped["unresolved"] += 1
            continue
        as_of_year = datetime.strptime(answer.as_of, "%m/%d/%Y").year
        # Delinquent = unpaid PRIOR years (same rule as the Snohomish roll); the
        # current year is shown separately and may not be overdue yet.
        prior = {y: v for y, v in answer.years.items() if y < as_of_year}
        if not prior:
            skipped["paid_or_redeemed"] += 1
            continue
        owner = _owner_matches(item.line, answer.owners)
        if owner is None:
            skipped["owner_not_verified"] += 1
            continue
        oldest = min(prior)
        rec = ScrapedRecord()
        rec.parcel_id = item.parcel
        rec.party_name = BridgeScraper.clean(owner)
        rec.property_address = answer.situs
        rec.mailing_address = answer.mailing
        rec.date_recorded = None  # a tax account has years, not a filing date
        rec.enrichment_data = {
            "source": SOURCE,
            "delinquent_amount": str(sum(prior.values()).quantize(Decimal("0.01"))),
            "bill_year": oldest,
            "delinquent_years": sorted(prior),
            "delinquent_year_count": len(prior),
            "oldest_tax_year": oldest,
            "current_year_balance": str(answer.years.get(as_of_year, Decimal("0")).quantize(Decimal("0.01"))),
            "as_of_date": answer.as_of,
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
            pacs_parcel.resolve_mailing, "benton", [p.parcel for p in listing.parcels],
            detail_parser=parse_tax_detail,
        )
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


