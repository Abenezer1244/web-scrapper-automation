"""Free county GIS REST API enrichment.

Most US counties run ArcGIS-based GIS portals with free, unauthenticated
REST APIs. This module queries those endpoints for parcel data.

Cost: $0 — no API key, no rate limits (be polite though).

Each county's GIS endpoint URL is stored in county_connectors.gis_endpoint.
The ArcGIS REST query format is standardized across all counties.
"""

import requests

from src.config import settings
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.enrichment.gis")

# ─── Known GIS endpoints (built-in, no DB lookup needed) ─────────────────────
# Format: {county_state: {endpoint, parcel_field, address_field, mailing_field, owner_field}}
_KNOWN_GIS_ENDPOINTS: dict[str, dict] = {
    "pierce_WA": {
        "endpoint": (
            "https://services2.arcgis.com/1UvBaQ5y1ubjUPmd"
            "/arcgis/rest/services/Tax_Parcels/FeatureServer/0/query"
        ),
        "parcel_field": "TaxParcelNumber",
        "address_field": "Site_Address",
        "mailing_fields": ["Delivery_Address", "City_State", "Zipcode"],
        "owner_field": "Business_Name",
        "out_fields": (
            "TaxParcelNumber,Site_Address,Delivery_Address,"
            "City_State,Zipcode,Business_Name,Land_Value,Taxable_Value"
        ),
    },
}


def enrich_parcel_gis(
    parcel_id: str,
    county: str,
    state: str,
    gis_endpoint: str | None = None,
) -> dict[str, str | None]:
    """Look up a parcel via free county ArcGIS REST API.

    Args:
        parcel_id: The assessor parcel number (APN).
        county: County slug (e.g. "pierce").
        state: 2-letter state code (e.g. "WA").
        gis_endpoint: Optional override URL. If None, looks up from known endpoints.

    Returns:
        Dict with: property_address, mailing_address. Any may be None.
    """
    if not settings.GIS_ENRICHMENT_ENABLED:
        return _empty()

    county_key = f"{county.lower()}_{state.upper()}"

    # Resolve GIS config: explicit endpoint OR known built-in
    gis_config = None
    if gis_endpoint:
        # Custom endpoint from county_connectors table
        gis_config = _make_generic_config(gis_endpoint)
    elif county_key in _KNOWN_GIS_ENDPOINTS:
        gis_config = _KNOWN_GIS_ENDPOINTS[county_key]

    if not gis_config:
        return _empty()

    endpoint = gis_config["endpoint"]
    parcel_field = gis_config["parcel_field"]
    out_fields = gis_config.get("out_fields", "*")

    # Strip dashes from parcel ID (some sources include them)
    apn_clean = parcel_id.replace("-", "").strip()

    params = {
        "where": f"{parcel_field}='{apn_clean}'",
        "outFields": out_fields,
        "returnGeometry": "false",
        "f": "json",
    }

    try:
        resp = requests.get(endpoint, params=params, timeout=10)

        if resp.status_code != 200:
            _logger.warning(
                "GIS API returned %d for parcel %s (%s)",
                resp.status_code, parcel_id, county_key,
            )
            return _empty()

        data = resp.json()
        return _parse_gis_response(data, gis_config)

    except requests.exceptions.Timeout:
        _logger.warning("GIS API timed out for parcel %s (%s)", parcel_id, county_key)
        return _empty()
    except Exception as exc:
        _logger.warning("GIS API error for parcel %s: %s", parcel_id, str(exc)[:80])
        return _empty()


def _parse_gis_response(data: dict, gis_config: dict) -> dict[str, str | None]:
    """Parse ArcGIS REST API response into enrichment fields."""
    features = data.get("features") or []
    if not features:
        return _empty()

    attrs = features[0].get("attributes") or {}

    # Property/situs address
    address_field = gis_config.get("address_field", "Site_Address")
    property_address = attrs.get(address_field) or None
    if property_address:
        property_address = property_address.strip()

    # Mailing address (may be multiple fields joined)
    mailing_fields = gis_config.get("mailing_fields", [])
    if mailing_fields:
        parts = []
        for field in mailing_fields:
            val = attrs.get(field)
            if val and str(val).strip():
                parts.append(str(val).strip())
        mailing_address = ", ".join(parts) if parts else None
    else:
        mailing_address = property_address  # Fallback to property address

    # Owner name (logged for enrichment_data, not in primary return)
    owner_field = gis_config.get("owner_field")
    owner_name = attrs.get(owner_field) if owner_field else None

    result = {
        "property_address": property_address,
        "mailing_address": mailing_address,
    }

    if property_address:
        _logger.info(
            "GIS enriched parcel: %s → %s (owner: %s)",
            attrs.get(gis_config.get("parcel_field", ""), "?"),
            property_address,
            owner_name or "?",
        )

    return result


def _make_generic_config(endpoint: str) -> dict:
    """Create a generic ArcGIS config for unknown counties.

    Most ArcGIS parcel layers use similar field names. This covers
    the most common patterns. If a county uses different names,
    add it to _KNOWN_GIS_ENDPOINTS.
    """
    return {
        "endpoint": endpoint,
        "parcel_field": "TaxParcelNumber",
        "address_field": "Site_Address",
        "mailing_fields": ["Delivery_Address", "City_State", "Zipcode"],
        "owner_field": "Business_Name",
        "out_fields": "*",
    }


def _empty() -> dict[str, str | None]:
    return {"property_address": None, "mailing_address": None}
