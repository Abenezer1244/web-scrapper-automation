"""King County (WA) — Code Violations scraper via Seattle Open Data Socrata API.

Source: City of Seattle SDCI — Code Complaints and Violations
API: Socrata (data.seattle.gov), dataset ez4a-iug7
Docs: https://data.seattle.gov/w/ez4a-iug7

Covers City of Seattle code enforcement (weeds, junk, building, noise, etc.)
Properties facing code violations are motivated sellers — facing fines and
repair orders. Easier to sell as-is.

NOTE: Only covers City of Seattle, not all of King County.
No parcel ID in API — relies on GIS enrichment from address/coordinates.
"""

from datetime import datetime

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get

_logger = setup_logger("scraper.king_wa_code_violation")

_API_URL = "https://data.seattle.gov/resource/ez4a-iug7.json"
_HEADERS = {"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"}

add_scrape_domain("data.seattle.gov")


class KingWACodeViolationScraper(BridgeScraper):
    """Scrapes code violation records from Seattle's Socrata open data API.

    No browser automation needed — pure HTTP GET.
    Returns records with property address, case info, and coordinates.
    Parcel IDs are enriched via GIS from the address.
    """

    def __init__(self, record_type: str = "code_violation"):
        super().__init__()

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        start = datetime.strptime(date_from, "%m/%d/%Y")
        end = datetime.strptime(date_to, "%m/%d/%Y")

        where = (
            f"opendate >= '{start.strftime('%Y-%m-%dT00:00:00')}' "
            f"AND opendate <= '{end.strftime('%Y-%m-%dT23:59:59')}'"
        )

        _logger.info("King WA (Seattle) code violations — %s to %s", date_from, date_to)

        all_records: list[ScrapedRecord] = []
        seen: set[str] = set()
        offset = 0
        page_size = 1000

        while True:
            params = {
                "$where": where,
                "$order": "opendate DESC",
                "$limit": page_size,
                "$offset": offset,
            }

            try:
                # S4: safe_http (SSRF defense-in-depth). Fixed HTTPS Socrata
                # endpoint, but safe_get re-validates (resolve=True), disables
                # ambient proxy, and refuses redirect-to-internal. Same API.
                resp = safe_get(_API_URL, params=params, headers=_HEADERS, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                _logger.warning("API error at offset %d: %s", offset, str(exc)[:80])
                break

            if not data:
                break

            for item in data:
                rec_num = item.get("recordnum", "")
                if not rec_num or rec_num in seen:
                    continue
                seen.add(rec_num)

                record = ScrapedRecord()

                # Address
                addr = (item.get("originaladdress1") or "").strip()
                city = (item.get("originalcity") or "").strip()
                state = (item.get("originalstate") or "").strip()
                zipcode = (item.get("originalzip") or "").strip()
                if addr:
                    record.property_address = addr
                    if city:
                        record.property_address += f", {city}"
                    if state:
                        record.property_address += f" {state}"
                    if zipcode:
                        record.property_address += f" {zipcode}"

                # Date
                opendate = item.get("opendate", "")
                if opendate:
                    try:
                        dt = datetime.fromisoformat(opendate.replace("Z", "+00:00").split(".")[0])
                        record.date_recorded = dt.strftime("%m/%d/%Y")
                    except Exception as exc:
                        _logger.debug(
                            "Could not parse opendate=%r for %s: %s",
                            opendate, rec_num, exc,
                        )

                # Party name — case type + address (no owner name in API)
                desc = (item.get("recordtypedesc") or item.get("description") or "").strip()
                record.party_name = f"{desc} — {addr}" if addr else desc

                # Legal description — record number
                record.legal_description = rec_num

                # Enrichment data
                record.enrichment_data = {
                    "source": "seattle_sdci_code_violations",
                    "record_number": rec_num,
                    "record_type": item.get("recordtype"),
                    "description": (item.get("description") or "")[:200],
                    "status": item.get("statuscurrent"),
                    "last_inspection": item.get("lastinspdate"),
                    "last_result": item.get("lastinspresult"),
                    "latitude": item.get("latitude"),
                    "longitude": item.get("longitude"),
                }

                if record.date_recorded:
                    all_records.append(record)

            _logger.info("Fetched %d records (offset=%d, total=%d)", len(data), offset, len(all_records))

            if self.on_progress:
                self.on_progress(offset // page_size + 1, 0, len(all_records))

            if len(data) < page_size:
                break
            offset += page_size

        _logger.info("King WA (Seattle) code violations complete — %d records", len(all_records))
        return all_records

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass
