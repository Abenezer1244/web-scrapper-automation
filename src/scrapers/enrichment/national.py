"""National property data enrichment via Regrid API.

Replaces ALL per-county enrichment (ATIP, CAPTCHA, Playwright) with
a single API call that works for every county in every US state.

Regrid API: GET /api/v2/parcels/apn?parcelnumb=APN&token=TOKEN
Returns: GeoJSON with property address, mailing address, owner name,
assessed value, and more.
"""

import requests

from src.config import settings
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get

_logger = setup_logger("scraper.enrichment.national")

_REGRID_BASE = "https://app.regrid.com/api/v2/parcels/apn"


def enrich_parcel_national(
    parcel_id: str,
    county: str | None = None,
    state: str | None = None,
) -> dict[str, str | None]:
    """Look up a parcel by APN using Regrid's national API.

    Works for ANY county in ANY US state. No CAPTCHA, no Playwright,
    no per-county code needed.

    Args:
        parcel_id: The assessor parcel number (APN).
        county: Optional county slug (for path scoping).
        state: Optional 2-letter state code (for path scoping).

    Returns:
        Dict with: property_address, mailing_address, owner_name,
        assessed_value. Any may be None if not found.
    """
    if not settings.REGRID_ENABLED or not settings.REGRID_API_TOKEN:
        return _empty()

    params = {
        "parcelnumb": parcel_id,
        "token": settings.REGRID_API_TOKEN,
        "limit": 1,
    }

    # Scope to state/county if provided (faster + more accurate)
    if state:
        path = f"/us/{state.lower()}"
        if county:
            path += f"/{county.lower()}"
        params["path"] = path

    try:
        # S4: route through safe_http (SSRF defense-in-depth). _REGRID_BASE is
        # a fixed HTTPS vendor endpoint, but safe_get re-validates with
        # resolve=True, disables ambient proxy (trust_env=False), and blocks
        # redirect-to-internal — so a poisoned 302 to a metadata IP can't be
        # followed. Returns a requests.Response, so handling below is unchanged.
        resp = safe_get(_REGRID_BASE, params=params, timeout=15)

        if resp.status_code == 200:
            data = resp.json()
            return _parse_regrid_response(data)

        if resp.status_code == 401:
            _logger.error("Regrid API: invalid token")
            return _empty()

        if resp.status_code == 429:
            _logger.warning("Regrid API: rate limited")
            return _empty()

        _logger.warning("Regrid API returned %d for parcel %s", resp.status_code, parcel_id)
        return _empty()

    except requests.exceptions.Timeout:
        _logger.warning("Regrid API timed out for parcel %s", parcel_id)
        return _empty()
    except Exception as exc:
        _logger.warning("Regrid API error for parcel %s: %s", parcel_id, str(exc)[:60])
        return _empty()


def _parse_regrid_response(data: dict) -> dict[str, str | None]:
    """Parse Regrid GeoJSON response into enrichment fields."""
    features = data.get("features") or []
    if not features:
        return _empty()

    props = features[0].get("properties") or {}

    # Property/situs address
    property_address = props.get("address") or None
    if property_address:
        property_address = property_address.strip()

    # Mailing address
    # Only a real `mailadd` counts; a missing one is UNKNOWN, never the situs
    # (2026-09-02 policy: no assumed owner-occupancy).
    mailing_address = props.get("mailadd") or None
    if mailing_address:
        mailing_address = mailing_address.strip() or None

    # Owner name
    owner_name = props.get("owner") or None
    if owner_name:
        owner_name = owner_name.strip()

    # Assessed value
    assessed_value = props.get("parval") or None

    result = {
        "property_address": property_address,
        "mailing_address": mailing_address,
    }

    if owner_name or assessed_value:
        _logger.info(
            "Regrid enriched %s: %s (owner: %s, value: %s)",
            props.get("parcelnumb", "?"),
            property_address or "no address",
            owner_name or "?",
            assessed_value or "?",
        )

    return result


def _empty() -> dict[str, str | None]:
    return {"property_address": None, "mailing_address": None}
