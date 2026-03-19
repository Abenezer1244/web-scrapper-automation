"""County-agnostic parcel enrichment pipeline.

Each county connector calls enrich_parcel() with a parcel_id.
Pierce County: ATIP REST API (with circuit breaker — skips all parcels
if the API is detected as unavailable).
New counties plug in their own lookup while keeping the same interface.
"""

import asyncio
from typing import Any

import requests

from src.api.middleware.security import add_scrape_domain
from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.enrichment")

# Register approved enrichment domains (SSRF allowlist)
add_scrape_domain("atip.piercecountywa.gov")

# ─── Circuit breaker: skip enrichment for the rest of the job if API is down ──

_api_down: dict[str, bool] = {}


def _is_api_down(county_key: str) -> bool:
    return _api_down.get(county_key, False)


def _mark_api_down(county_key: str) -> None:
    _api_down[county_key] = True
    _logger.warning("Enrichment API marked DOWN for %s — skipping remaining parcels", county_key)


# ─── Pierce County ATIP ───────────────────────────────────────────────────────

_ATIP_API_URL = "https://atip.piercecountywa.gov/api/parcelSearch/search"
_ATIP_HEADERS = {
    "User-Agent": "BridgeLeads-Enrichment/1.0",
    "Accept": "application/json",
}

_EMPTY = {"property_address": None, "mailing_address": None}
_UNAVAILABLE = {"property_address": "(enrichment unavailable)", "mailing_address": "(enrichment unavailable)"}


def _parse_atip_response(data: dict[str, Any]) -> dict[str, str | None]:
    """Extract property_address and mailing_address from ATIP API response."""
    parcel = data.get("parcel") or {}
    site = parcel.get("siteAddress") or {}
    mail = parcel.get("mailingAddress") or {}

    def _join(*parts: str | None) -> str | None:
        joined = " ".join(p for p in parts if p and str(p).strip())
        return joined.strip() or None

    property_address = _join(
        site.get("streetAddress"),
        site.get("city"),
        site.get("state"),
        site.get("zip"),
    )
    mailing_address = _join(
        mail.get("streetAddress"),
        mail.get("city"),
        mail.get("state"),
        mail.get("zip"),
    )
    return {
        "property_address": property_address,
        "mailing_address": mailing_address,
    }


async def _enrich_pierce_api(parcel_id: str) -> dict[str, str | None] | None:
    """Fetch parcel data from Pierce County ATIP REST API.

    Returns parsed address dict on success, None if the API is unavailable.
    Uses a single attempt with fast failure — no retries on non-JSON responses.
    """
    params = {"parcelNumber": parcel_id}

    try:
        resp = requests.get(
            _ATIP_API_URL,
            params=params,
            headers=_ATIP_HEADERS,
            timeout=10,
        )

        # Check if we got HTML instead of JSON (API returns SPA page when down)
        content_type = resp.headers.get("content-type", "")
        if "html" in content_type:
            _logger.warning("ATIP API returned HTML instead of JSON — API is down")
            return None

        if resp.status_code == 200:
            data = resp.json()
            return _parse_atip_response(data)

        if resp.status_code == 429:
            _logger.warning("ATIP rate-limited for parcel %s", parcel_id)
            await asyncio.sleep(2)
            return None

        _logger.warning("ATIP API returned %d for parcel %s", resp.status_code, parcel_id)
        return None

    except requests.exceptions.JSONDecodeError:
        _logger.warning("ATIP API returned non-JSON for parcel %s", parcel_id)
        return None
    except requests.exceptions.Timeout:
        _logger.warning("ATIP API timed out for parcel %s", parcel_id)
        return None
    except Exception as exc:
        _logger.warning("ATIP API error for parcel %s: %s", parcel_id, exc)
        return None


# ─── Public interface ─────────────────────────────────────────────────────────

async def enrich_parcel(parcel_id: str, county: str, state: str) -> dict[str, str | None]:
    """Enrich a parcel record with property and mailing address data.

    Routes to the correct county lookup based on county + state.
    Uses a circuit breaker: if the first parcel fails, skips all remaining
    parcels in the same job to avoid wasting time.

    Args:
        parcel_id: The county parcel identifier (e.g. '0001000001').
        county: Lowercase county slug (e.g. 'pierce').
        state: Uppercase state code (e.g. 'WA').

    Returns:
        Dict with keys: property_address, mailing_address (either may be None).
    """
    county_key = f"{county.lower()}_{state.upper()}"

    # Circuit breaker: skip if API already known to be down
    if _is_api_down(county_key):
        return _UNAVAILABLE

    _logger.info("Enriching parcel %s (%s, %s)", parcel_id, county, state)

    if county_key == "pierce_WA":
        result = await _enrich_pierce_api(parcel_id)
        if result is None:
            _mark_api_down(county_key)
            return _UNAVAILABLE
        return result

    # Unknown county — return empty enrichment rather than error
    _logger.warning("No enrichment handler for %s — skipping", county_key)
    return _EMPTY
