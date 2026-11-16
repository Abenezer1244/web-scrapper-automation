"""Cowlitz County (WA) -- tax-delinquent parcels in the Treasurer's tax foreclosure.

Source: the Treasurer's yearly "<YYYY> Foreclosure" page (linked from the Foreclosure
index) and the listing PDF it links, "Foreclosure Property Information":

    Foreclosure Property Information
    Last Update 11/5/2026 1:40:24 PM  Count = 7
    Cause Number 26-2-00582-08
    Parcel/Prop ID  Record Title Holder  Address  Acreage  Value  ...  Minimum Bid
    WL2408005 3018024 BLEVINS TERESA JANELLE ETAL 5228 OCEAN BEACH HWY LONGVIEW, WA ...
    ...
    3018024: https://apps.co.cowlitz.wa.us/Foreclosures/2026/3018024_WL2408005.pdf

The Certificate of Delinquency named 13 parcels (taxes from 2023 or earlier, RCW 84.64);
the listing is re-issued as parcels redeem (live 2026-11-15: 7 left, updated 11/5).
So the listing itself is the current status: a parcel on it is still in foreclosure as
of its "Last Update", and a stale listing is refused rather than read as current.

The cycle page states the sale: "The tentative auction date for all parcels, except for
parcel number 080980081, is December 7, 2026." An excepted parcel has no date yet. Once a
listed parcel's (tentative) date has passed, the listing no longer says what happened to
it (sold, redeemed, postponed), so the run FAILS until the Treasurer publishes the
outcome or the next cycle: never a quiet zero.

Owner: the record title holder the Treasurer printed, confirmed against the county
Assessor's own parcel layer (DEED_HOLDER_NAME for the same parcel). A row whose parsed
holder the layer does not confirm fails the run (a parse that split a name wrongly, or
a changed owner, must not ship as verified).

Amount: the minimum bid is the statutory redemption total (taxes, interest, penalties
and costs, RCW 84.64.080), stored with that basis. The oldest unpaid year per parcel is
not published, so it is left empty rather than guessed. Real property only.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from urllib.parse import urljoin

from src.api.middleware.security import add_scrape_domain
from src.config import settings
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.scrapers.sources.nts_pdf import extract_pdf_text
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get

_logger = setup_logger("scraper.cowlitz_wa_tax_delinquent")

SOURCE = "cowlitz_county_tax_foreclosure"
_HOST = "www.co.cowlitz.wa.us"
INDEX_URL = f"https://{_HOST}/1010/Foreclosure"
_HEADERS = {"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"}
_MAX_PDF_BYTES = 5_000_000
# The listing is re-issued as parcels redeem; one not touched for this long is not
# evidence of current status.
MAX_LISTING_AGE_DAYS = 45

add_scrape_domain(_HOST)
add_scrape_domain("apps.co.cowlitz.wa.us")

_CYCLE_LINK_RE = re.compile(r'href="(/\d+/(\d{4})-Foreclosure)"', re.I)
_LISTING_LINK_RE = re.compile(r'href="(/DocumentCenter/View/\d+/[^"]*Foreclosure-Property-Information[^"]*)"', re.I)
_SALE_RE = re.compile(
    r"auction date for all parcels(?:,\s*except for parcels?(?: numbers?)?\s+([\w ,and]+?))?,\s*is\s+"
    r"([A-Z][a-z]+ \d{1,2}, \d{4})", re.I)
_UPDATED_RE = re.compile(r"Last Update\s+(\d{1,2}/\d{1,2}/\d{4})")
_COUNT_RE = re.compile(r"Count\s*=\s*(\d+)")
_CAUSE_RE = re.compile(r"Cause Number\s+([\w-]+)")
_TITLE_LINK_RE = re.compile(r"(\d+):\s*https://apps\.co\.cowlitz\.wa\.us/Foreclosures/\d{4}/(\d+)_(\w+)\.pdf")
_BID_RE = re.compile(r"(\d[\d,]*\.\d{2})\s*$")
_STREET_START_RE = re.compile(r"\s(\d+\s+[A-Z0-9])")


@dataclass
class ListedParcel:
    parcel: str
    property_id: str
    holder: str
    situs: str | None
    minimum_bid: Decimal
    title_report_url: str


@dataclass
class Cycle:
    year: int
    sale_date: date | None
    excepted: set[str] = field(default_factory=set)
    listing_url: str = ""


@dataclass
class Listing:
    parcels: list[ListedParcel]
    last_update: date
    cause_number: str | None
    year: int | None = None   # the title reports' cycle folder (/Foreclosures/<year>/)


def newest_cycle_url(index_html: str) -> tuple[int, str]:
    """The newest "<YYYY> Foreclosure" page linked from the index, or raise."""
    found = {int(y): path for path, y in _CYCLE_LINK_RE.findall(index_html)}
    if not found:
        raise RuntimeError("Cowlitz Treasurer index links no yearly foreclosure page")
    year = max(found)
    return year, urljoin(INDEX_URL, found[year])


def parse_cycle(page_html: str, year: int, page_url: str) -> Cycle:
    """Sale date (and parcels excepted from it) and the listing link, or raise."""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page_html.replace("&nbsp;", " ")))
    link = _LISTING_LINK_RE.search(page_html)
    if link is None:
        raise RuntimeError(f"Cowlitz {year} foreclosure page links no property listing")
    sale = _SALE_RE.search(text)
    if sale is None:
        raise RuntimeError(f"Cowlitz {year} foreclosure page states no auction date")
    excepted = set(re.findall(r"\b(?=\w*\d)\w{5,}\b", sale.group(1) or ""))
    return Cycle(year=year, sale_date=datetime.strptime(sale.group(2), "%B %d, %Y").date(),
                 excepted=excepted, listing_url=urljoin(page_url, link.group(1)))


def parse_listing(text: str) -> Listing:
    """The listing PDF's text -> its parcels, or ValueError when it does not reconcile:
    the row count, the stated Count and the title-report links must all agree."""
    updated = _UPDATED_RE.search(text)
    count = _COUNT_RE.search(text)
    if updated is None or count is None:
        raise ValueError("Cowlitz foreclosure listing: no 'Last Update' or 'Count' header")
    raw_links = [(lm.group(3), lm.group(2), lm.group(0).split("/Foreclosures/")[1])
                 for lm in _TITLE_LINK_RE.finditer(text)]
    links = {parcel: (prop, f"https://apps.co.cowlitz.wa.us/Foreclosures/{path}")
             for parcel, prop, path in raw_links}
    if len(links) != len(raw_links) or len({prop for _p, prop, _x in raw_links}) != len(raw_links):
        raise ValueError("Cowlitz foreclosure listing: a parcel or property id appears twice")
    lines = [ln.strip() for ln in text.splitlines()]
    starts = [i for i in range(len(lines) - 1)
              if lines[i] in links and lines[i + 1] == links[lines[i]][0]]
    stated = int(count.group(1))
    if not (len(starts) == len(links) == stated):
        raise ValueError(f"Cowlitz foreclosure listing states {stated} parcels; "
                         f"{len(starts)} rows and {len(links)} title reports parsed")
    # The table ends where the link list begins ("<parcel>: https://gis...").
    tail = next((i for i, ln in enumerate(lines) if re.match(r"^\w+:\s*https://", ln)), len(lines))
    parcels = []
    for n, i in enumerate(starts):
        block = lines[i + 2:(starts[n + 1] if n + 1 < len(starts) else tail)]
        parcels.append(_row(lines[i], links[lines[i]], block))
    years = {path.split("/")[0] for _p, _q, path in raw_links}
    return Listing(parcels=parcels,
                   last_update=datetime.strptime(updated.group(1), "%m/%d/%Y").date(),
                   cause_number=(_CAUSE_RE.search(text) or [None, None])[1],
                   year=int(years.pop()) if len(years) == 1 else None)


def _row(parcel: str, link: tuple[str, str], block: list[str]) -> ListedParcel:
    """One listing row: holder lines until the situs street ("<number> <street>", on
    its own line or after the name) or "Bare Land" / "Jurisdiction:"; the bid is the
    block's last amount."""
    holder: list[str] = []
    situs: list[str] = []
    for ln in block:
        if ln.startswith("Jurisdiction:") or "Bare Land" in ln:
            if "Bare Land" in ln:
                holder.append(ln.split("Bare Land")[0])
            break
        m = _STREET_START_RE.search(" " + ln)
        if m:
            holder.append(ln[:m.start(1) - 1])
            situs = [ln[m.start(1) - 1:]]  # the street only; city and ZIP come from GIS
            break
        holder.append(ln)
    bid = next((m.group(1) for m in (_BID_RE.search(ln) for ln in reversed(block)) if m), None)
    name = " ".join(" ".join(holder).split())
    if not name or bid is None:
        raise ValueError(f"Cowlitz foreclosure listing: row {parcel} has no holder or minimum bid")
    return ListedParcel(parcel=parcel, property_id=link[0], holder=name,
                        situs=" ".join(" ".join(situs).split()) or None,
                        minimum_bid=Decimal(bid.replace(",", "")), title_report_url=link[1])


def _norm_name(name: str | None) -> str:
    return " ".join(re.sub(r"[^A-Z0-9& ]", " ", (name or "").upper()).split())


def _holder_confirmed(holder: str, deed_holder: str | None) -> bool:
    """The parsed holder IS the Assessor's deed holder for the same parcel: equal once
    an "ETAL" / "ET AL" suffix is set aside. A prefix is not enough: "ABC" against
    "ABC 123 HOLDINGS LLC" is the mis-split this check exists to catch (Codex P1)."""
    def core(name: str | None) -> str:
        return re.sub(r"\s+ET\s*AL$", "", _norm_name(name))

    a, b = core(holder), core(deed_holder)
    return bool(a) and a == b


_GIS_LAYER = "https://gis.cowlitzwa.gov/ccserver/rest/services/Assessor/Parcels/MapServer/0/query"


def fetch_deed_holders(parcels: list[str]) -> dict[str, str]:
    """parcel -> DEED_HOLDER_NAME from the Cowlitz Assessor's parcel layer, or raise."""
    from src.scrapers.enrichment.county_gis import _arcgis_literal

    if not parcels:
        return {}
    params = {"where": f"PARCNO IN ({','.join(_arcgis_literal(p) for p in parcels)})",
              "outFields": "PARCNO,DEED_HOLDER_NAME", "returnGeometry": "false", "f": "json"}
    resp = safe_get(_GIS_LAYER, params=params, require_allowlisted=False, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or data.get("error") or "features" not in data:
        raise RuntimeError("Cowlitz Assessor parcel layer did not answer")
    return {f["attributes"]["PARCNO"]: f["attributes"].get("DEED_HOLDER_NAME") or ""
            for f in data["features"]}


def build_records(cycle: Cycle, listing: Listing, today: date,
                  deed_holders: dict[str, str]) -> tuple[list[ScrapedRecord], dict]:
    """Current leads: every listed parcel, its holder confirmed by the Assessor layer
    (``deed_holders``: parcel -> DEED_HOLDER_NAME). Raises when the listing is not
    current status (stale, future-dated, another cycle's, or past a parcel's sale
    date) or a holder is not confirmed."""
    age = (today - listing.last_update).days
    if not 0 <= age <= MAX_LISTING_AGE_DAYS:
        raise RuntimeError(f"Cowlitz foreclosure listing last updated {listing.last_update} "
                           f"({age} days): too old (or future-dated) to be current status")
    if listing.year != cycle.year:
        raise RuntimeError(f"Cowlitz foreclosure listing is for {listing.year}, the page for {cycle.year}")
    passed = [p.parcel for p in listing.parcels
              if p.parcel not in cycle.excepted and cycle.sale_date < today]
    if passed:
        raise RuntimeError(f"Cowlitz {cycle.year} tax sale date {cycle.sale_date} has passed but "
                           f"{len(passed)} parcels are still listed: outcome not published")
    unconfirmed = [p.parcel for p in listing.parcels
                   if not _holder_confirmed(p.holder, deed_holders.get(p.parcel))]
    if unconfirmed:
        raise RuntimeError("Cowlitz foreclosure listing: title holder not confirmed by the "
                           f"Assessor layer for parcel(s) {', '.join(unconfirmed)}")
    records: list[ScrapedRecord] = []
    skipped: dict[str, int] = {}
    for p in listing.parcels:
        sale = None if p.parcel in cycle.excepted else cycle.sale_date
        rec = ScrapedRecord()
        rec.parcel_id = p.parcel
        rec.party_name = BridgeScraper.clean(p.holder)
        rec.property_address = p.situs
        rec.date_recorded = None  # a foreclosure listing has a cycle, not a filing date
        rec.enrichment_data = {
            "source": SOURCE,
            "delinquent_amount": str(p.minimum_bid.quantize(Decimal("0.01"))),
            "delinquent_amount_basis": "foreclosure_minimum_bid",
            "bill_year": None,
            "as_of_date": listing.last_update.strftime("%m/%d/%Y"),
            "account_number": p.property_id,
            "county": "cowlitz",
            "state": "WA",
            "owner_verified": True,  # Treasurer's title holder = the Assessor's deed holder
            "owner_role": "record_title_holder",
            "foreclosure_status": ("tax_foreclosure_sale_scheduled" if sale
                                   else "tax_foreclosure_sale_date_pending"),
            "foreclosure_minimum_bid": str(p.minimum_bid),
            "foreclosure_sale_dates": [sale.strftime("%m/%d/%Y")] if sale else [],
            "foreclosure_cause_number": listing.cause_number,
            "foreclosure_listing_updated": listing.last_update.strftime("%m/%d/%Y"),
            "foreclosure_listing_url": cycle.listing_url,
            "title_report_url": p.title_report_url,
        }
        records.append(rec)
    return records, skipped


class CowlitzWATaxDelinquentScraper(BridgeScraper):
    """Cowlitz tax-foreclosure parcels still listed and not yet sold. Pure HTTP."""

    @classmethod
    def collection_scope(cls, record_type: str):
        from src.scrapers.doc_scope import dataset

        if record_type != "tax_delinquent":
            return None
        return dataset(
            "Collected from the Cowlitz County Treasurer's current tax-foreclosure property "
            "listing (parcels three or more years delinquent), as last updated by the Treasurer."
        )

    def __init__(self, record_type: str = "tax_delinquent") -> None:
        super().__init__()

    def _get(self, url: str, **kw):
        resp = safe_get(url, require_allowlisted=True, headers=_HEADERS,
                        timeout=settings.DEFAULT_TIMEOUT, **kw)
        resp.raise_for_status()
        return resp

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        # A current snapshot: the listing has no date range.
        del date_from, date_to
        self.report_stage("searching")
        year, cycle_url = newest_cycle_url(self._get(INDEX_URL).text)
        cycle = parse_cycle(self._get(cycle_url).text, year, cycle_url)
        pdf = self._get(cycle.listing_url, max_bytes=_MAX_PDF_BYTES)
        listing = parse_listing(extract_pdf_text(pdf.content))
        holders = await asyncio.to_thread(fetch_deed_holders, [p.parcel for p in listing.parcels])
        records, skipped = build_records(cycle, listing, datetime.now(UTC).date(), holders)
        _logger.info("Cowlitz tax foreclosure %d: %d listed (updated %s), %d leads, skipped %s",
                     year, len(listing.parcels), listing.last_update, len(records), skipped)
        if self.on_progress:
            self.on_progress(0, 0, len(records))
        return records

    async def __aenter__(self) -> CowlitzWATaxDelinquentScraper:
        return self

    async def __aexit__(self, *args) -> None:
        pass
