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

import random
import time
from datetime import UTC, datetime, timedelta

import requests

from src.api.middleware.security import add_scrape_domain
from src.config import settings
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get

_logger = setup_logger("scraper.pierce_wa_code_violation")

_FEATURE_URL = (
    "https://services3.arcgis.com/SCwJH1pD8WSn5T5y/arcgis/rest/services"
    "/Code%20Violations/FeatureServer/0/query"
)
_HEADERS = {"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"}

# Layer's unique object-id field (probed from FeatureServer/0 metadata:
# objectIdField=objectid). Used as the primary stable sort so resultOffset
# paging can't skip or duplicate rows under a non-unique sort key.
_OBJECTID_FIELD = "objectid"

_PAGE_SIZE = 500

# Per-page fetch retries for transient ArcGIS failures (read timeout / 429 /
# 5xx). Backoff seconds, indexed by attempt; jittered to desync shared-IP retries.
_RETRY_BACKOFF = (1, 3, 7)
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

add_scrape_domain("services3.arcgis.com")


class _ArcGISErrorBodyError(RuntimeError):
    """ArcGIS returned HTTP 200 with a JSON ``{"error": ...}`` body (or a body
    missing ``features``). These are usually transient (the service throttles /
    overloads and answers 200 with an error payload), so they are retried like a
    5xx before failing loud — distinct from a plain RuntimeError, which is not."""


def _is_retryable(exc: Exception) -> bool:
    """True for transient HTTP failures worth re-attempting (read timeout /
    connection drop / 429 / 5xx / ArcGIS 200-error-body); False for SSRF
    (ValueError) and non-transient 4xx (bad query / forbidden), which a retry
    can't fix."""
    if isinstance(exc, _ArcGISErrorBodyError):
        return True
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return exc.response.status_code in _RETRY_STATUS
    return False


class PierceWACodeViolationScraper(BridgeScraper):
    """Scrapes code violation records from Tacoma's ArcGIS REST API.

    No browser automation needed — uses HTTP GET to the public FeatureServer.
    Returns records with parcel ID, property address, case info.
    """

    def __init__(self, record_type: str = "code_violation"):
        super().__init__()

    def _fetch_page(self, params: dict, offset: int, page_num: int) -> dict:
        """Fetch one ArcGIS page with bounded retries on transient failures.

        Retries read-timeout / 429 / 5xx with jittered backoff
        (settings.MAX_RETRIES attempts). A failure that survives all retries — or
        any non-transient error — FAILS LOUD: a partial result would ship a
        truncated lead list the worker would mark DONE, so the job must fail
        rather than silently return fewer records.

        ArcGIS frequently returns HTTP 200 with a JSON error body
        ``{"error": {...}}`` instead of features — that is NOT "0 results", it is
        a failure, so it is raised, not swallowed.
        """
        last_exc: Exception | None = None
        for attempt in range(1, settings.MAX_RETRIES + 1):
            try:
                # S4: safe_http (SSRF defense-in-depth) — re-validates the fixed
                # HTTPS ArcGIS endpoint each attempt, disables ambient proxy,
                # refuses redirect-to-internal.
                resp = safe_get(_FEATURE_URL, params=params, headers=_HEADERS,
                                timeout=settings.DEFAULT_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                # ArcGIS returns 200 + an error body on a bad/overloaded query.
                # Treat a non-dict, an "error" key, or a missing "features" key as
                # a hard failure — never as an empty page.
                if not isinstance(data, dict) or "error" in data or "features" not in data:
                    err = ""
                    if isinstance(data, dict) and isinstance(data.get("error"), dict):
                        err = str(data["error"].get("message", data["error"]))[:160]
                    # _ArcGISErrorBodyError is retryable (usually a transient throttle);
                    # a persistent bad query still fails loud after MAX_RETRIES.
                    raise _ArcGISErrorBodyError(
                        f"ArcGIS returned an error/malformed body at offset {offset} "
                        f"(page {page_num + 1}): {err or str(data)[:160]}"
                    )
                return data
            except Exception as exc:
                last_exc = exc
                if attempt >= settings.MAX_RETRIES or not _is_retryable(exc):
                    break
                wait = _RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)]
                wait += random.uniform(0, 0.5)  # jitter to desync shared-IP retries
                _logger.warning(
                    "Pierce code-violation page fetch failed (offset=%d page=%d "
                    "attempt=%d/%d): %s — retrying in %.1fs",
                    offset, page_num + 1, attempt, settings.MAX_RETRIES,
                    str(exc)[:120], wait,
                )
                time.sleep(wait)
        raise RuntimeError(
            f"Pierce code violations: API page fetch failed at offset {offset} "
            f"(page {page_num + 1}) after {settings.MAX_RETRIES} attempt(s) — "
            f"aborting to avoid a truncated result: {str(last_exc)[:120]}"
        ) from last_exc

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        """Fetch code violations from ArcGIS API for the given date range."""
        # Parse dates (MM/DD/YYYY format from the job system)
        start = datetime.strptime(date_from, "%m/%d/%Y")
        end = datetime.strptime(date_to, "%m/%d/%Y")

        # Half-open range: [start 00:00, end+1day 00:00). The previous
        # ``opendate <= TIMESTAMP 'end'`` resolved to end-midnight and DROPPED
        # every case opened ON the end date; the upper exclusive next-midnight
        # bound includes the full end day with no millisecond loss.
        end_next = end + timedelta(days=1)
        where = (
            f"opendate >= TIMESTAMP '{start.strftime('%Y-%m-%d')} 00:00:00' "
            f"AND opendate < TIMESTAMP '{end_next.strftime('%Y-%m-%d')} 00:00:00'"
        )

        _logger.info("Pierce WA code violations — %s to %s", date_from, date_to)

        all_records: list[ScrapedRecord] = []
        seen: set[str] = set()
        offset = 0
        page_num = 0
        total_fetched = 0
        skipped_no_date = 0
        max_pages = 1000  # defensive guard against a pagination loop that never ends

        while True:
            if page_num >= max_pages:
                raise RuntimeError(
                    f"Pierce code violations: exceeded {max_pages} pages "
                    f"(offset={offset}) — aborting suspected pagination loop"
                )

            params = {
                "where": where,
                "outFields": "*",
                # Order by the unique objectid so resultOffset paging is stable;
                # casenumber dedup is the second line of defense against overlap.
                "orderByFields": f"{_OBJECTID_FIELD} ASC",
                "resultRecordCount": _PAGE_SIZE,
                "resultOffset": offset,
                "f": "json",
            }

            data = self._fetch_page(params, offset, page_num)

            features = data.get("features", [])
            if not features:
                break

            total_fetched += len(features)

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

                # Date — epoch milliseconds to MM/DD/YYYY. Parse in UTC so the
                # recorded day is deterministic regardless of the host timezone
                # (a naive fromtimestamp uses the SERVER's local tz → boundary drift).
                open_ts = attr.get("opendate")
                if open_ts:
                    try:
                        dt = datetime.fromtimestamp(open_ts / 1000, tz=UTC)
                        record.date_recorded = dt.strftime("%m/%d/%Y")
                    except Exception as exc:
                        # Don't swallow silently: log + count so a systematic date
                        # problem is visible (and the canary below covers an all-skip).
                        _logger.debug(
                            "Could not parse opendate=%r for case %s: %s",
                            open_ts, case_num, exc,
                        )

                # Party name — case type + address from STRUCTURED fields only.
                # There is no owner name in the API, and we deliberately do NOT use
                # any free-text complaint field (it can carry complainant PII).
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
                else:
                    skipped_no_date += 1

            _logger.info("Fetched %d records (offset=%d, total=%d)", len(features), offset, len(all_records))

            page_num += 1
            if self.on_progress:
                self.on_progress(page_num, 0, len(all_records))

            # Continue while ArcGIS signals more rows (exceededTransferLimit) OR a
            # full page came back; stop only when neither holds (server returned a
            # short page and did not flag a transfer-limit cut).
            exceeded = bool(data.get("exceededTransferLimit"))
            if not exceeded and len(features) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE

        if skipped_no_date:
            _logger.warning(
                "Pierce code violations: %d of %d fetched features had an unparseable/"
                "missing opendate and were dropped",
                skipped_no_date, total_fetched,
            )

        # Structural canary: a sizeable fetch that yields zero records means the
        # parse/dedup broke or the source changed shape — fail loud, don't ship empty.
        if total_fetched >= 100 and not all_records:
            raise RuntimeError(
                f"Pierce code violations fetched {total_fetched} features but produced "
                f"0 records — likely a parse bug or source-format change"
            )

        _logger.info("Pierce WA code violations complete — %d records", len(all_records))
        return all_records

    async def __aenter__(self):
        """No browser needed — just return self."""
        return self

    async def __aexit__(self, *args):
        """No browser to close."""
        pass
