"""King County (WA) — Tax Delinquent scraper via Socrata Open Data API.

Source: King County Open Data — Delinquent Taxes dataset
API: Socrata (data.kingcounty.gov), dataset dsv3-ct3e

Returns properties with unpaid/delinquent taxes. These owners are motivated
sellers — facing tax liens, penalties, and eventual foreclosure.

The API returns account_number (12-digit tax account). The parcel ID is
derived as the first 10 digits (Major 6 + Minor 4). No owner name or address
in the API — relies on GIS + eRealProperty enrichment.
"""

from datetime import datetime

import requests

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.king_wa_tax_delinquent")

_API_URL = "https://data.kingcounty.gov/resource/dsv3-ct3e.json"
_HEADERS = {"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"}

add_scrape_domain("data.kingcounty.gov")


class KingWATaxDelinquentScraper(BridgeScraper):
    """Scrapes tax delinquent records from King County's Socrata open data API.

    No browser automation needed — pure HTTP GET.
    Returns unique delinquent accounts with parcel IDs and delinquent amounts.
    Owner name and addresses are enriched via GIS + eRealProperty.
    """

    def __init__(self, record_type: str = "tax_delinquent"):
        super().__init__()

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        start = datetime.strptime(date_from, "%m/%d/%Y")
        end = datetime.strptime(date_to, "%m/%d/%Y")

        # Filter by receivable_type='D' (delinquent) and bill_year in range
        start_year = start.year
        end_year = end.year
        where = f"receivable_type='D' AND bill_year>='{start_year}' AND bill_year<='{end_year}'"

        _logger.info("King WA tax delinquent — years %d to %d", start_year, end_year)

        all_records: list[ScrapedRecord] = []
        seen_accounts: set[str] = set()
        offset = 0
        page_size = 1000

        while True:
            params = {
                "$where": where,
                "$order": "bill_year DESC, billed_amount DESC",
                "$limit": page_size,
                "$offset": offset,
            }

            try:
                resp = requests.get(_API_URL, params=params, headers=_HEADERS, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                _logger.warning("API error at offset %d: %s", offset, str(exc)[:80])
                break

            if not data:
                break

            for item in data:
                acct = (item.get("account_number") or "").strip()
                if not acct or acct in seen_accounts:
                    continue
                seen_accounts.add(acct)

                record = ScrapedRecord()

                # Derive parcel ID from account number (first 10 digits)
                # King County format: Major (6) + Minor (4) + suffix (2)
                parcel = acct[:10] if len(acct) >= 10 else acct
                record.parcel_id = parcel

                # Bill year as date
                bill_year = item.get("bill_year", "")
                record.date_recorded = f"01/01/{bill_year}" if bill_year else None

                # Amounts (stored as zero-padded strings)
                billed_raw = item.get("billed_amount", "0")
                paid_raw = item.get("paid_amount", "0")
                try:
                    billed = int(billed_raw) / 100  # cents to dollars
                    paid = int(paid_raw) / 100
                    delinquent = billed - paid
                except (ValueError, TypeError):
                    billed = 0
                    paid = 0
                    delinquent = 0

                # Party name — will be enriched from eRealProperty
                # For now, use account + delinquent amount
                record.party_name = f"Tax Delinquent — ${delinquent:,.0f} owed (Acct {acct})"

                # Legal description — account number
                record.legal_description = acct

                # Enrichment data
                record.enrichment_data = {
                    "source": "king_county_delinquent_taxes",
                    "account_number": acct,
                    "bill_year": bill_year,
                    "billed_amount": billed,
                    "paid_amount": paid,
                    "delinquent_amount": delinquent,
                    "levy_code": item.get("levy_code"),
                    "account_status": item.get("account_status"),
                }

                if record.parcel_id:
                    all_records.append(record)

            _logger.info("Fetched %d rows (offset=%d, unique accounts=%d)",
                         len(data), offset, len(all_records))

            if self.on_progress:
                self.on_progress(offset // page_size + 1, 0, len(all_records))

            if len(data) < page_size:
                break
            offset += page_size

        _logger.info("King WA tax delinquent complete — %d unique accounts", len(all_records))
        return all_records

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass
