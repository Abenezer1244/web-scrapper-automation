"""The contract every King code-violation source adapter meets, plus shared ArcGIS paging.

An adapter is one jurisdiction's public code-enforcement feed. It:
  * has a stable `key` (enrichment_data.source) and a `jurisdiction` label for the
    customer-facing scope note;
  * implements `fetch(date_from, date_to)` (MM/DD/YYYY, both inclusive) returning
    ScrapedRecord objects with a per-case raw_html_hash of sha256("{key}|{case id}")[:32];
  * retries its own transient failures (settings.MAX_RETRIES, settings.DEFAULT_TIMEOUT)
    and RAISES when a failure survives them, never returning a truncated list;
  * keeps its own structural canary: a sizeable scan that parses to nothing raises.

The connector decides what one source's failure means for the job; an adapter only
reports it by raising.
"""
from __future__ import annotations

import random
import re
import time
from abc import ABC, abstractmethod

import requests

from src.config import settings
from src.scrapers.base_scraper import ProgressCallback, ScrapedRecord
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get

_logger = setup_logger("scraper.king_cv_sources")

HEADERS = {"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"}

# Backoff seconds per attempt, jittered to desync shared-IP retries (same as SDCI/Tacoma).
_RETRY_BACKOFF = (1, 3, 7)
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

# A scan at least this large that parses to zero records is a source-format break, not an
# empty window (the threshold SDCI and Tacoma already use).
CANARY_MIN_ROWS = 100

# Defensive page guard: a service that never returns a short final page cannot loop forever.
MAX_PAGES = 1000

# Cap stored free-ish labels so a runaway source value cannot bloat a row.
LABEL_MAX = 120

_PIN_SEPARATORS = re.compile(r"[\s\-]")


class DateRangeTooLargeError(RuntimeError):
    """The date range holds more than one run of this source can collect.

    Running again later would fail the same way, so the connector tells the customer to
    use a shorter date range instead.
    """


def normalize_king_pin(raw: object) -> str | None:
    """A King County PIN as the 10-digit string the Assessor uses, else None.

    Dashes and spaces are dropped ("338990-0395" -> "3389900395"). Leading zeros are kept
    because the value is never treated as a number, and nothing is padded or guessed: a
    value that is not exactly 10 digits is not a PIN we can prove.
    """
    if raw is None:
        return None
    pin = _PIN_SEPARATORS.sub("", str(raw))
    return pin if len(pin) == 10 and pin.isdigit() else None


class ArcGISErrorBodyError(RuntimeError):
    """ArcGIS answered HTTP 200 with an ``{"error": ...}`` body or no ``features``.

    Usually a throttle or overload, so it is retried like a 5xx; a persistent bad query
    still fails loud once the retries are spent.
    """


def is_retryable(exc: Exception) -> bool:
    """True for transient failures (timeout, dropped connection, 429/5xx, ArcGIS error
    body); False for SSRF refusals (ValueError) and other 4xx, which a retry cannot fix."""
    if isinstance(exc, ArcGISErrorBodyError):
        return True
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return exc.response.status_code in _RETRY_STATUS
    return False


def get_json_with_retries(url: str, params: dict, *, what: str, require_features: bool) -> dict:
    """GET one ArcGIS endpoint as JSON with bounded retries; raise when it cannot answer.

    ``require_features`` is True for a layer query (a body without ``features`` is a
    failure, never "0 results") and False for layer metadata.
    """
    last_exc: Exception | None = None
    for attempt in range(1, settings.MAX_RETRIES + 1):
        try:
            # SSRF defense in depth: the fixed HTTPS endpoint is re-validated on every
            # attempt and must be on the scrape allowlist the adapter registered.
            resp = safe_get(url, params=params, headers=HEADERS, timeout=settings.DEFAULT_TIMEOUT,
                            require_allowlisted=True)
            resp.raise_for_status()
            data = resp.json()
            if (not isinstance(data, dict) or "error" in data
                    or (require_features and "features" not in data)):
                err = ""
                if isinstance(data, dict) and isinstance(data.get("error"), dict):
                    err = str(data["error"].get("message", data["error"]))[:160]
                raise ArcGISErrorBodyError(
                    f"{what}: ArcGIS returned an error or malformed body: {err or str(data)[:160]}")
            return data
        except Exception as exc:
            last_exc = exc
            if attempt >= settings.MAX_RETRIES or not is_retryable(exc):
                break
            wait = _RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)] + random.uniform(0, 0.5)
            _logger.warning("%s failed (attempt %d/%d): %s; retrying in %.1fs",
                            what, attempt, settings.MAX_RETRIES, str(exc)[:120], wait)
            time.sleep(wait)
    raise RuntimeError(
        f"{what} failed after {settings.MAX_RETRIES} attempt(s); aborting this source rather "
        f"than returning a truncated result: {str(last_exc)[:160]}"
    ) from last_exc


def arcgis_query_all(url: str, params: dict, *, page_size: int, what: str,
                     on_page=None) -> list[dict]:
    """Every feature's attributes for one ArcGIS query, paged with resultOffset.

    ``params`` must carry an ``orderByFields`` that makes the order total, or offset
    paging can skip or repeat rows at page boundaries. Paging continues while the server
    flags ``exceededTransferLimit`` or returns a full page.
    """
    rows: list[dict] = []
    offset = 0
    for page_num in range(MAX_PAGES):
        data = get_json_with_retries(
            url, {**params, "resultRecordCount": page_size, "resultOffset": offset, "f": "json"},
            what=f"{what} page {page_num + 1} (offset {offset})", require_features=True)
        features = data.get("features") or []
        rows.extend((f.get("attributes") or {}) for f in features)
        if on_page is not None:
            on_page(page_num + 1, len(rows))
        if not features or (not data.get("exceededTransferLimit") and len(features) < page_size):
            return rows
        offset += page_size
    raise RuntimeError(f"{what}: hit the {MAX_PAGES}-page guard without a short final page")


class CodeViolationSource(ABC):
    """One jurisdiction's code-enforcement feed (see the module docstring for the contract)."""

    #: enrichment_data.source for every record this adapter returns.
    key: str
    #: How the jurisdiction reads in "Collected from the code enforcement records of ...".
    jurisdiction: str

    def __init__(self) -> None:
        self.on_progress: ProgressCallback | None = None

    def _progress(self, pages: int, records: int) -> None:
        if self.on_progress is not None:
            self.on_progress(pages, 0, records)

    @abstractmethod
    async def fetch(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        """Cases opened between date_from and date_to (MM/DD/YYYY, inclusive)."""

    def check_canary(self, fetched: int, parsed: int) -> None:
        """Raise when a sizeable scan parsed to nothing: a field rename or format change."""
        if fetched >= CANARY_MIN_ROWS and parsed == 0:
            raise RuntimeError(
                f"{self.key}: scanned {fetched} rows but none parsed to a case with a date; "
                f"likely a source-format change"
            )
