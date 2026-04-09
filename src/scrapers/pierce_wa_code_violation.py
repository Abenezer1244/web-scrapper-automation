"""Pierce County (WA) — Code Violations scraper via Tacoma ArcGIS REST API.

Source: City of Tacoma Open Data — Code Violations feature layer
API: ArcGIS FeatureServer (no browser needed — pure HTTP)

Covers City of Tacoma code enforcement cases (nuisance, zoning, building, etc.)
Properties facing code violations are motivated sellers — facing fines, repair
orders, and potential liens. Easier to sell as-is.

Fields returned:
- casenumber, casetype, address, parcelnumber
- opendate, currentstatus, inspector
- latitude, longitude, parcelinfo (ATIP link)
"""

import re
from datetime import datetime

import requests

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.pierce_wa_code_violation")

_FEATURE_URL = (
    "https://services3.arcgis.com/SCwJH1pD8WSn5T5y/arcgis/rest/services"
    "/Code%20Violations/FeatureServer/0/query"
)
_HEADERS = {"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"}

add_scrape_domain("services3.arcgis.com")


class PierceWACodeViolationScraper(BridgeScraper):
    """Scrapes code violation records from Tacoma's ArcGIS REST API.

    No browser automation needed — uses HTTP GET to the public FeatureServer.
    Returns records with parcel ID, property address, case info.
    """

    def __init__(self, record_type: str = "code_violation"):
        super().__init__()

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        """Fetch code violations from ArcGIS API for the given date range."""
        # Parse dates (MM/DD/YYYY format from the job system)
        start = datetime.strptime(date_from, "%m/%d/%Y")
        end = datetime.strptime(date_to, "%m/%d/%Y")

        where = (
            f"opendate >= TIMESTAMP '{start.strftime('%Y-%m-%d')}' "
            f"AND opendate <= TIMESTAMP '{end.strftime('%Y-%m-%d')}'"
        )

        _logger.info("Pierce WA code violations — %s to %s", date_from, date_to)

        all_records: list[ScrapedRecord] = []
        seen: set[str] = set()
        offset = 0
        page_size = 500

        while True:
            params = {
                "where": where,
                "outFields": "*",
                "orderByFields": "opendate DESC",
                "resultRecordCount": page_size,
                "resultOffset": offset,
                "f": "json",
            }

            try:
                resp = requests.get(_FEATURE_URL, params=params, headers=_HEADERS, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                _logger.warning("API error at offset %d: %s", offset, str(exc)[:80])
                break

            features = data.get("features", [])
            if not features:
                break

            for feat in features:
                attr = feat.get("attributes", {})
                case_num = str(attr.get("casenumber", ""))
                if not case_num or case_num in seen:
                    continue
                seen.add(case_num)

                record = ScrapedRecord()

                # Parcel ID
                parcel = str(attr.get("parcelnumber", "")).strip()
                if parcel and len(parcel) >= 6:
                    record.parcel_id = parcel

                # Property address
                address = (attr.get("address") or "").strip()
                if address:
                    record.property_address = address

                # Date — epoch milliseconds to MM/DD/YYYY
                open_ts = attr.get("opendate")
                if open_ts:
                    try:
                        dt = datetime.fromtimestamp(open_ts / 1000)
                        record.date_recorded = dt.strftime("%m/%d/%Y")
                    except Exception:
                        pass

                # Party name — use the description which often has the address/owner info
                # For code violations, there's no "owner name" in the API.
                # We store the case type + address as the party identifier.
                case_type = (attr.get("casetype") or "").strip()
                status = (attr.get("currentstatus") or "").strip()
                record.party_name = f"{case_type} — {address}" if address else case_type

                # Legal description — use case number
                record.legal_description = case_num

                # Store all metadata
                record.enrichment_data = {
                    "source": "tacoma_code_violations",
                    "case_number": case_num,
                    "case_type": case_type,
                    "status": status,
                    "inspector": attr.get("inspector"),
                    "days_open": attr.get("daysopentoclosed"),
                    "latitude": attr.get("latitude"),
                    "longitude": attr.get("longitude"),
                    "parcel_info_url": attr.get("parcelinfo"),
                    "customer_number": str(attr.get("customernumber", "")),
                }

                if record.date_recorded:
                    all_records.append(record)

            _logger.info("Fetched %d records (offset=%d, total=%d)", len(features), offset, len(all_records))

            if self.on_progress:
                self.on_progress(offset // page_size + 1, 0, len(all_records))

            # Check if there are more
            if len(features) < page_size:
                break
            offset += page_size

        _logger.info("Pierce WA code violations complete — %d records", len(all_records))
        return all_records

    async def __aenter__(self):
        """No browser needed — just return self."""
        return self

    async def __aexit__(self, *args):
        """No browser to close."""
        pass
