"""Clallam County (WA) — Tax Delinquent leads from the Treasurer's tax-foreclosure list.

Source 1 — the Treasurer's Certificate of Delinquency (parcels three or more years
delinquent, headed for the tax-foreclosure sale), linked from
    https://www.clallamcountywa.gov/1077/Tax-Foreclosure-Sales
as "2026 Certificate of Delinquency - Filed with Superior Court on 9/4/2026" and then
"Updated Certificate - 11.06.2026" as parcels are redeemed. The newest dated link is
the current list; the DocumentCenter id rotates, so it is resolved at run time.

Source 2 — the county's own property portal (PACS), one page per parcel, for the
CURRENT status. A certificate only proves the parcel was on the list on its date, so
every parcel is re-read on the portal and kept only if it still owes prior-year tax:
    https://websrv22.clallam.net/propertyaccess/Property.aspx?cid=0&prop_id=<PID>
The portal must print the certificate's own 16-digit Geographic ID (zeros kept).

This is the foreclosure stage of delinquency, not the county's whole delinquent roll
(Clallam publishes no such roll). Verified 2026-11-12: 25 parcels on the 9/4 list,
8 on the 11/6 list; a dropped parcel's page reads "There is currently No Amount Due".
"""
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from src.api.middleware.security import add_scrape_domain
from src.api.tax_filters import TAX_CAP_EXEMPT_SOURCES, tax_cap_min_year
from src.config import settings
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.scrapers.enrichment.pacs import compose_pacs_mailing, parse_pacs_detail_html
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get, safe_get_following

_logger = setup_logger("scraper.clallam_wa_tax_delinquent")

_SOURCE = "clallam_county_tax_foreclosure_certificate"
_LANDING_URL = "https://www.clallamcountywa.gov/1077/Tax-Foreclosure-Sales"
_PORTAL = "https://websrv22.clallam.net/propertyaccess/Property.aspx"
_HEADERS = {"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"}
for _url in (_LANDING_URL, _PORTAL):
    add_scrape_domain(urlparse(_url).hostname)

# A certificate older than this is not the current list (the county files one a year).
_MAX_CERT_AGE_DAYS = 400

_LINK_RE = re.compile(r'<a[^>]+href="([^"]*DocumentCenter/View/\d+[^"]*)"[^>]*>(.*?)</a>', re.I | re.S)
_ROW_RE = re.compile(r"(\d{2} \d{2} \d{2} \d{6} \d{4}) \$([\d,]+\.\d\d)(.*?)//\s*PID\s*(\d+)", re.S)
_AS_OF_RE = re.compile(r"\b(\d{2})-([A-Za-z]{3})-(\d{2})\b")
_NO_AMOUNT_DUE = "There is currently No Amount Due on this property"
_TAX_AS_OF_RE = re.compile(r"Property Tax Information as of\s*(\d{1,2}/\d{1,2}/\d{4})")
_DUE_ROW_RE = re.compile(r"^(\d{4})\s*-\s*\d+")


def _anchor_date(text: str) -> date | None:
    """The date in a certificate link's text: '11.06.2026' or 'on 9/4/2026'."""
    m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", text)
    return date(int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else None


def select_certificate(html: str, base_url: str) -> tuple[str, date, date]:
    """(url of the newest certificate, its date, the original filing date).

    Raises ValueError when the page links no certificate: an empty list must never
    be read as "no delinquent parcels"."""
    certs: list[tuple[date, str, str]] = []
    for href, inner in _LINK_RE.findall(html):
        text = " ".join(re.sub(r"<[^>]+>", " ", inner).split())
        if "certificate" not in text.lower():
            continue
        when = _anchor_date(text)
        if when:
            certs.append((when, urljoin(base_url, href), text))
    if not certs:
        raise ValueError("Clallam tax-foreclosure page links no dated certificate")
    filed = [c for c in certs if "filed" in c[2].lower()]
    newest = max(certs)
    filing_date = max(filed)[0] if filed else newest[0]
    return newest[1], newest[0], filing_date


def parse_certificate(text: str) -> tuple[date, list[dict]]:
    """(as-of date, rows) from the certificate PDF text. Raises on an incomplete read."""
    m = _AS_OF_RE.search(text)
    if not m:
        raise ValueError("certificate has no as-of date")
    as_of = datetime.strptime(f"{m.group(1)}-{m.group(2)}-{m.group(3)}", "%d-%b-%y").date()
    rows = [
        {"parcel": parcel.replace(" ", ""), "parcel_raw": parcel,
         "cert_amount": amount.replace(",", ""), "pid": pid,
         "cert_situs": " ".join(situs.split())}
        for parcel, amount, situs, pid in _ROW_RE.findall(text)
    ]
    blocks = text.count("Described as:")
    if not rows or len(rows) != blocks:
        raise ValueError(f"certificate read {len(rows)} parcels of {blocks} listed")
    return as_of, rows


def _money(raw: str) -> Decimal:
    try:
        return Decimal(raw.replace("$", "").replace(",", "").strip())
    except InvalidOperation as exc:
        raise ValueError(f"unreadable amount {raw!r}") from exc


def parse_portal_status(html: str) -> dict:
    """One portal page -> {geo_ids, owner, mailing, situs, as_of, balances}.

    ``balances`` is {year: total due}; {} when the page says no amount is due.
    Raises ValueError when the page shows neither the tax table nor that sentence."""
    soup = BeautifulSoup(html, "html.parser")
    detail = parse_pacs_detail_html(html)
    text = " ".join(soup.get_text(" ").split())
    m = _TAX_AS_OF_RE.search(text)
    as_of = datetime.strptime(m.group(1), "%m/%d/%Y").date() if m else None
    owner = None
    for td in soup.find_all("td"):
        if td.get_text(" ", strip=True) == "Name:":
            value = td.find_next_sibling("td")
            owner = " ".join(value.get_text(" ").split()) if value else None
            break
    balances: dict[int, Decimal] = {}
    if _NO_AMOUNT_DUE not in text:
        table = next((t for t in soup.find_all("table")
                      if "Year - Statement ID" in t.get_text(" ") and "Total Due" in t.get_text(" ")), None)
        if table is None:
            raise ValueError("portal page shows neither a tax-due table nor 'No Amount Due'")
        for tr in table.find_all("tr"):
            cells = [" ".join(td.get_text(" ").split()) for td in tr.find_all("td")]
            year = _DUE_ROW_RE.match(cells[0]) if cells else None
            if year:
                balances[int(year.group(1))] = balances.get(int(year.group(1)), Decimal(0)) + _money(cells[-1])
        if not balances:
            raise ValueError("portal tax-due table has no year rows")
    return {
        "geo_ids": [g.replace(" ", "") for g in detail["geo_ids"]],
        "owner": owner,
        "mailing": compose_pacs_mailing(detail["mailing"][0]) if detail["mailing"] else None,
        "situs": ", ".join(detail["situs"]) if detail["situs"] else None,
        "as_of": as_of,
        "balances": balances,
    }


def build_record(row: dict, status: dict, cert: dict, cap_min_year: int | None) -> tuple[ScrapedRecord | None, str]:
    """(record, outcome). outcome names why a parcel is not a lead."""
    if row["parcel"] not in status["geo_ids"]:
        return None, "identity_mismatch"
    if status["as_of"] is None:
        raise ValueError(f"PID {row['pid']}: portal page has no tax as-of date")
    prior = {y: amt for y, amt in status["balances"].items() if y < status["as_of"].year and amt > 0}
    if not prior:
        return None, "no_prior_year_balance"   # redeemed, sold, or paid down
    bill_year = min(prior)
    if cap_min_year is not None and bill_year < cap_min_year:
        return None, "capped_out"
    if not status["owner"]:
        raise ValueError(f"PID {row['pid']}: portal page has no owner name")
    rec = ScrapedRecord()
    rec.parcel_id = row["parcel"]          # 16 digits as a string; never truncated
    rec.party_name = BridgeScraper.clean(status["owner"])
    rec.property_address = status["situs"]
    rec.mailing_address = status["mailing"]
    rec.date_recorded = cert["filed"].strftime("%m/%d/%Y")   # certificate filed with the court
    rec.enrichment_data = {
        "source": _SOURCE,
        "delinquent_amount": str(sum(prior.values()).quantize(Decimal("0.01"))),
        "bill_year": bill_year,
        "delinquent_years": sorted(prior),
        "oldest_tax_year": bill_year,
        "tax_status_as_of": status["as_of"].isoformat(),
        "stage": "tax_foreclosure_certificate",
        "certificate_url": cert["url"],
        "certificate_as_of": cert["as_of"].isoformat(),
        "certificate_filed": cert["filed"].isoformat(),
        "certificate_amount": row["cert_amount"],
        "certificate_parcel": row["parcel_raw"],
        "certificate_situs": row["cert_situs"],
        "portal_prop_id": row["pid"],
        "county": "clallam",
        "state": "WA",
    }
    return rec, "lead"


class ClallamWATaxDelinquentScraper(BridgeScraper):
    """Clallam tax-foreclosure parcels that still owe prior-year tax today.

    Pure HTTP (no browser), like the King and Snohomish tax scrapers."""

    @classmethod
    def collection_scope(cls, record_type: str):
        from src.scrapers.doc_scope import dataset

        if record_type != "tax_delinquent":
            return None
        return dataset(
            "Collected from the Clallam County Treasurer's tax-foreclosure certificate, "
            "with each parcel's current balance read from the county property portal."
        )

    def __init__(self, record_type: str = "tax_delinquent") -> None:
        super().__init__()

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        # A current snapshot, not dated filings: the window does not apply.
        del date_from, date_to
        today = datetime.now(UTC).date()
        self.report_stage("searching")
        landing = safe_get(_LANDING_URL, require_allowlisted=True, headers=_HEADERS,
                           timeout=settings.DEFAULT_TIMEOUT)
        landing.raise_for_status()
        url, linked, filed = select_certificate(landing.text, _LANDING_URL)

        import pypdf

        # DocumentCenter links redirect to the file; every hop is re-validated.
        pdf = safe_get_following(url, require_allowlisted=True, require_https=True, headers=_HEADERS,
                                 timeout=settings.DEFAULT_TIMEOUT, max_bytes=settings.MAX_DOWNLOAD_BYTES)
        pdf.raise_for_status()
        if "pdf" not in pdf.headers.get("content-type", "").lower():
            raise ValueError(f"certificate link {url} did not return a PDF")
        text = "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(BytesIO(pdf.content)).pages)
        as_of, rows = parse_certificate(text)
        if (today - as_of).days > _MAX_CERT_AGE_DAYS:
            raise ValueError(f"newest Clallam certificate is from {as_of}; no current list")
        cert = {"url": url, "as_of": as_of, "filed": filed}
        _logger.info("Clallam certificate %s (as of %s, filed %s): %d parcels", url, as_of, filed, len(rows))

        cap_min_year = None if _SOURCE in TAX_CAP_EXEMPT_SOURCES else tax_cap_min_year(today)
        records: list[ScrapedRecord] = []
        outcomes: dict[str, int] = {}
        for i, row in enumerate(rows, 1):
            page = safe_get(_PORTAL, require_allowlisted=True, headers=_HEADERS,
                            params={"cid": "0", "year": str(today.year), "prop_id": row["pid"]},
                            timeout=settings.DEFAULT_TIMEOUT)
            page.raise_for_status()
            record, outcome = build_record(row, parse_portal_status(page.text), cert, cap_min_year)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            if outcome == "identity_mismatch":
                _logger.warning("PID %s does not show certificate parcel %s; not delivered",
                                row["pid"], row["parcel"])
            if record:
                records.append(record)
            if self.on_progress:
                self.on_progress(i, len(rows), len(records))
            await self.polite_delay()
        _logger.info("Clallam tax delinquent complete — %d parcels: %s (cap_min_year=%s)",
                     len(rows), outcomes, cap_min_year)
        return records

    async def __aenter__(self) -> "ClallamWATaxDelinquentScraper":
        return self

    async def __aexit__(self, *args) -> None:
        pass
