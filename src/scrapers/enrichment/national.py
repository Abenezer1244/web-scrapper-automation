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
        resp = requests.get(_REGRID_BASE, params=params, timeout=15)

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
    mailing_address = props.get("mailadd") or None
    if mailing_address:
        mailing_address = mailing_address.strip()
    elif property_address:
        # Fallback: use property address if no separate mailing
        mailing_address = property_address

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
