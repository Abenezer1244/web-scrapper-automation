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
        "owner_field": "Legal_Description",
        "out_fields": (
            "TaxParcelNumber,Site_Address,Delivery_Address,"
            "City_State,Zipcode,Business_Name,Legal_Description,"
            "Land_Value,Taxable_Value,Longitude,Latitude"
        ),
    },
}

# ─── Statewide GIS endpoints (covers ALL counties in a state) ────────────────
# WA State publishes all 39 counties in a single ArcGIS service.
# FIPS codes map county names to their FIPS number for filtering.
_WA_STATEWIDE_ENDPOINT = (
    "https://services.arcgis.com/jsIt88o09Q0r1j8h"
    "/arcgis/rest/services/Current_Parcels/FeatureServer/0/query"
)

_WA_COUNTY_FIPS: dict[str, str] = {
    "adams": "001", "asotin": "003", "benton": "005", "chelan": "007",
    "clallam": "009", "clark": "011", "columbia": "013", "cowlitz": "015",
    "douglas": "017", "ferry": "019", "franklin": "021", "garfield": "023",
    "grant": "025", "grays harbor": "027", "island": "029", "jefferson": "031",
    "king": "033", "kitsap": "035", "kittitas": "037", "klickitat": "039",
    "lewis": "041", "lincoln": "043", "mason": "045", "okanogan": "047",
    "pacific": "049", "pend oreille": "051", "pierce": "053", "san juan": "055",
    "skagit": "057", "skamania": "059", "snohomish": "061", "spokane": "063",
    "stevens": "065", "thurston": "067", "wahkiakum": "069", "walla walla": "071",
    "whatcom": "073", "whitman": "075", "yakima": "077",
}


def enrich_parcel_gis(
    parcel_id: str,
    county: str,
    state: str,
    gis_endpoint: str | None = None,
    owner_name: str | None = None,
) -> dict[str, str | None]:
    """Look up a parcel via free county ArcGIS REST API.

    Args:
        parcel_id: The assessor parcel number (APN).
        county: County slug (e.g. "pierce").
        state: 2-letter state code (e.g. "WA").
        gis_endpoint: Optional override URL. If None, looks up from known endpoints.
        owner_name: Optional owner/party name for name-based fallback search.

    Returns:
        Dict with: property_address, mailing_address. Any may be None.
    """
    if not settings.GIS_ENRICHMENT_ENABLED:
        return _empty()

    county_key = f"{county.lower()}_{state.upper()}"

    # Resolve GIS config: explicit endpoint OR known built-in
    gis_config = None
    if gis_endpoint:
        gis_config = _make_generic_config(gis_endpoint)
    elif county_key in _KNOWN_GIS_ENDPOINTS:
        gis_config = _KNOWN_GIS_ENDPOINTS[county_key]

    # Try county-specific endpoint first (by parcel ID)
    if gis_config:
        result = _query_gis(parcel_id, gis_config, county_key)
        if result.get("property_address"):
            return result

        # Fallback: search by owner name if parcel ID didn't match
        if owner_name and gis_config.get("owner_field"):
            result = _query_gis_by_name(owner_name, gis_config, county_key)
            if result.get("property_address"):
                _logger.info("GIS name-based fallback succeeded for %s", owner_name)
                return result

    # Fallback: WA statewide parcel service (covers all 39 WA counties)
    if state.upper() == "WA":
        result = _query_wa_statewide(parcel_id, county)
        if result.get("property_address"):
            return result

    return _empty()


def _query_gis(parcel_id: str, gis_config: dict, county_key: str) -> dict[str, str | None]:
    """Query a county-specific ArcGIS REST endpoint."""
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


def _query_gis_by_name(owner_name: str, gis_config: dict, county_key: str) -> dict[str, str | None]:
    """Fallback: search GIS by owner/business name when parcel ID doesn't match."""
    endpoint = gis_config["endpoint"]
    owner_field = gis_config.get("owner_field", "Business_Name")
    out_fields = gis_config.get("out_fields", "*")

    # Clean name: take last name only for broader match
    name_clean = owner_name.strip().upper().split(",")[0].split(" ")[0]
    if len(name_clean) < 3:
        return _empty()

    params = {
        "where": f"{owner_field} LIKE '{name_clean}%'",
        "outFields": out_fields,
        "returnGeometry": "false",
        "resultRecordCount": 1,
        "f": "json",
    }

    try:
        resp = requests.get(endpoint, params=params, timeout=10)
        if resp.status_code != 200:
            return _empty()

        data = resp.json()
        return _parse_gis_response(data, gis_config)

    except Exception as exc:
        _logger.warning("GIS name search error for %s: %s", owner_name, str(exc)[:60])
        return _empty()


def _query_wa_statewide(parcel_id: str, county: str) -> dict[str, str | None]:
    """Query the WA statewide parcel service (covers all 39 WA counties).

    Endpoint: WAGeoservices Current_Parcels FeatureServer
    Fields: ORIG_PARCEL_ID, SITUS_ADDRESS, SITUS_CITY_NM, SITUS_ZIP_NR
    Filter: FIPS_NR for county scoping.
    """
    apn_clean = parcel_id.replace("-", "").strip()
    fips = _WA_COUNTY_FIPS.get(county.lower())

    where_clause = f"ORIG_PARCEL_ID='{apn_clean}'"
    if fips:
        where_clause += f" AND FIPS_NR='{fips}'"

    params = {
        "where": where_clause,
        "outFields": "ORIG_PARCEL_ID,SITUS_ADDRESS,SITUS_CITY_NM,SITUS_ZIP_NR,VALUE_LAND,VALUE_BLDG",
        "returnGeometry": "false",
        "f": "json",
        "resultRecordCount": 1,
    }

    try:
        resp = requests.get(_WA_STATEWIDE_ENDPOINT, params=params, timeout=15)

        if resp.status_code != 200:
            _logger.warning("WA statewide GIS returned %d for parcel %s", resp.status_code, parcel_id)
            return _empty()

        data = resp.json()
        features = data.get("features") or []
        if not features:
            return _empty()

        attrs = features[0].get("attributes") or {}
        address = attrs.get("SITUS_ADDRESS") or None
        city = attrs.get("SITUS_CITY_NM") or ""
        zipcode = attrs.get("SITUS_ZIP_NR") or ""

        if address:
            address = address.strip()
            # Build full mailing address
            parts = [address]
            if city:
                parts.append(city.strip())
            if zipcode:
                parts.append(f"WA {str(zipcode).strip()}")
            mailing = ", ".join(parts)

            _logger.info("WA statewide GIS enriched parcel %s: %s", parcel_id, address)
            return {"property_address": address, "mailing_address": mailing}

        return _empty()

    except requests.exceptions.Timeout:
        _logger.warning("WA statewide GIS timed out for parcel %s", parcel_id)
        return _empty()
    except Exception as exc:
        _logger.warning("WA statewide GIS error for parcel %s: %s", parcel_id, str(exc)[:80])
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
