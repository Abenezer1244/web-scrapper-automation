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

import random
import time
from datetime import datetime

import requests

from src.api.middleware.security import add_scrape_domain
from src.config import settings
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get

_logger = setup_logger("scraper.king_wa_code_violation")

_API_URL = "https://data.seattle.gov/resource/ez4a-iug7.json"
_HEADERS = {"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"}

add_scrape_domain("data.seattle.gov")

_PAGE_SIZE = 1000

# Defensive cap so a misbehaving API that never returns a short final page can't
# loop forever (1000 pages × _PAGE_SIZE = 1M rows, far beyond any real window).
_MAX_PAGES = 1000

# Cap the structured party label so a runaway recordtypedesc can't bloat the row.
_LABEL_MAX = 120

# Per-page fetch retries for transient Socrata failures (read timeout / 429 /
# 5xx). Backoff seconds, indexed by attempt; jittered to desync shared-IP retries.
_RETRY_BACKOFF = (1, 3, 7)
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def _is_retryable(exc: Exception) -> bool:
    """True for transient HTTP failures worth re-attempting (read timeout /
    connection drop / 429 / 5xx); False for SSRF (ValueError) and non-transient
    4xx (bad query / forbidden), which a retry can't fix."""
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return exc.response.status_code in _RETRY_STATUS
    return False


class KingWACodeViolationScraper(BridgeScraper):
    """Scrapes code violation records from Seattle's Socrata open data API.

    No browser automation needed — pure HTTP GET.
    Returns records with property address, case info, and coordinates.
    Parcel IDs are enriched via GIS from the address.
    """

    @classmethod
    def collection_scope(cls, record_type: str):
        """SHOW descriptor — King code violations come from a dataset, not docs."""
        from src.scrapers.doc_scope import dataset

        if record_type != "code_violation":
            return None
        return dataset(
            "Collected from Seattle's SDCI code-violation open dataset; recorder "
            "document-type filtering is not used."
        )

    def __init__(self, record_type: str = "code_violation"):
        super().__init__()

    def _fetch_page(self, params: dict, offset: int, page_num: int) -> list:
        """Fetch one Socrata page with bounded retries on transient failures.

        Retries read-timeout / 429 / 5xx with jittered backoff
        (settings.MAX_RETRIES attempts). A failure that survives all retries — or
        any non-transient error (SSRF ValueError, 4xx) — FAILS LOUD: a transient
        error here would otherwise truncate the list and the worker would mark the
        job DONE on a partial/empty result, so abort instead of shipping that.
        """
        last_exc: Exception | None = None
        for attempt in range(1, settings.MAX_RETRIES + 1):
            try:
                # S4: safe_http (SSRF defense-in-depth) — re-validates the fixed
                # HTTPS Socrata endpoint each attempt, disables ambient proxy,
                # refuses redirect-to-internal.
                resp = safe_get(_API_URL, params=params, headers=_HEADERS,
                                timeout=settings.DEFAULT_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                # Socrata returns a JSON list of rows. A non-list (200 carrying an
                # error object) means the query/source changed shape — fail loud.
                if not isinstance(data, list):
                    raise RuntimeError(
                        f"King code violation: expected a JSON list at offset "
                        f"{offset} (page {page_num + 1}) but got {type(data).__name__}"
                    )
                return data
            except Exception as exc:
                last_exc = exc
                if attempt >= settings.MAX_RETRIES or not _is_retryable(exc):
                    break
                wait = _RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)]
                wait += random.uniform(0, 0.5)  # jitter to desync shared-IP retries
                _logger.warning(
                    "King code violation page fetch failed (offset=%d page=%d "
                    "attempt=%d/%d): %s — retrying in %.1fs",
                    offset, page_num + 1, attempt, settings.MAX_RETRIES,
                    str(exc)[:120], wait,
                )
                time.sleep(wait)
        raise RuntimeError(
            f"King code violation: API page fetch failed at offset {offset} "
            f"(page {page_num + 1}) after {settings.MAX_RETRIES} attempt(s) — "
            f"aborting to avoid a truncated result: {str(last_exc)[:120]}"
        ) from last_exc

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
        page_num = 0
        total_fetched = 0  # raw rows seen across all pages (F6 structural canary)
        skipped_no_date = 0

        while True:
            # Defensive max-page guard (checked before the fetch, matching Pierce):
            # a misbehaving API that never returns a short page can't loop forever.
            if page_num >= _MAX_PAGES:
                raise RuntimeError(
                    f"King code violation: hit max-page guard ({_MAX_PAGES} pages) "
                    f"without a short final page — aborting to avoid an infinite loop"
                )

            params = {
                "$where": where,
                # Order by ``:id`` — the dataset's unique, indexed system row id.
                # ``opendate`` is NON-unique, so ties reorder at page boundaries and
                # ``$offset`` paging can skip or duplicate rows; ``:id`` is the only
                # stable key for offset paging here (and avoids a server-side sort).
                "$order": ":id",
                "$limit": _PAGE_SIZE,
                "$offset": offset,
            }

            # Raises (RuntimeError) on transient-after-retries, non-retryable, or
            # non-list response — never silently truncates the result.
            data = self._fetch_page(params, offset, page_num)
            if not data:
                break
            total_fetched += len(data)

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

                # Date — robust parse of the Socrata ISO timestamp. Strip a trailing
                # Z, drop fractional seconds, then fromisoformat the YYYY-MM-DDTHH:MM:SS
                # head. Unparseable dates are skipped but counted (F6 canary covers
                # the case where many rows fetch but all date-skip).
                opendate = item.get("opendate", "")
                if opendate:
                    try:
                        head = opendate.rstrip("Z").split(".")[0]
                        dt = datetime.fromisoformat(head)
                        record.date_recorded = dt.strftime("%m/%d/%Y")
                    except Exception as exc:
                        _logger.debug(
                            "Could not parse opendate=%r for %s: %s",
                            opendate, rec_num, exc,
                        )

                # Party name — case type + address (no owner name in API). Build the
                # label from STRUCTURED fields ONLY; NEVER fall back to `description`
                # (a free-text complainant narrative carrying tenant PII).
                label = (
                    item.get("recordtypedesc")
                    or item.get("recordtype")
                    or "Code Violation"
                ).strip()[:_LABEL_MAX]
                record.party_name = f"{label} — {addr}" if addr else label

                # Legal description — record number
                record.legal_description = rec_num

                # Enrichment data — structured fields only (no `description`: it
                # persists complainant PII).
                record.enrichment_data = {
                    "source": "seattle_sdci_code_violations",
                    "record_number": rec_num,
                    "record_type": item.get("recordtype"),
                    "status": item.get("statuscurrent"),
                    "last_inspection": item.get("lastinspdate"),
                    "last_result": item.get("lastinspresult"),
                    "latitude": item.get("latitude"),
                    "longitude": item.get("longitude"),
                }

                if record.date_recorded:
                    all_records.append(record)
                else:
                    skipped_no_date += 1

            page_num += 1
            _logger.info(
                "Fetched %d records (page=%d, offset=%d, total=%d)",
                len(data), page_num, offset, len(all_records),
            )

            if self.on_progress:
                self.on_progress(page_num, 0, len(all_records))

            if len(data) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE

        if skipped_no_date:
            _logger.warning(
                "King code violation: %d of %d fetched rows had an unparseable/"
                "missing opendate and were dropped",
                skipped_no_date, total_fetched,
            )

        # Structural canary: a sizeable scan (>=100 rows) that emits zero records
        # means a parse/shape break (e.g. field rename, all dates unparseable), not
        # a genuinely empty window — fail loud rather than ship empty.
        if total_fetched >= 100 and not all_records:
            raise RuntimeError(
                f"King code violation scanned {total_fetched} rows but produced 0 "
                f"records — likely a parse bug or source-format change"
            )

        _logger.info("King WA (Seattle) code violations complete — %d records", len(all_records))
        return all_records

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass
