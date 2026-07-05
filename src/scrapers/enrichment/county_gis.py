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
from src.utils.safe_http import safe_get

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
    # King County — Assessor parcel layer (KingCo_PropertyInfo/2). PIN is a
    # 10-char string with leading zeros PRESERVED (never strip to int). ADDR_FULL
    # is the situs street; POSTALCTYNAME/STATE_ABBR/ZIP5 complete the mailable
    # address (address_suffix_fields). King withholds the taxpayer/mailing fields
    # (KCTP_*) from public GIS, so there is NO bulk mailing here: mailing_fields=[]
    # AND echo_property_to_mailing=False — mailing stays NULL unless the per-parcel
    # eRealProperty pass (king_county_assessor.py) finds a REAL taxpayer mailing.
    # Echoing property into mailing would (a) misrepresent absentee owners and (b)
    # block that upgrade pass (which only runs for rows with no mailing). ~1/3 of
    # delinquent parcels are vacant/raw land with ADDR_FULL=null — those are
    # returned matched (vacant_no_situs) so they DON'T fall through to the WA
    # statewide service and pick up a wrong address.
    "king_WA": {
        "endpoint": (
            "https://gismaps.kingcounty.gov/arcgis/rest/services"
            "/Property/KingCo_PropertyInfo/MapServer/2/query"
        ),
        "parcel_field": "PIN",
        "address_field": "ADDR_FULL",
        "address_suffix_fields": ["POSTALCTYNAME", "STATE_ABBR", "ZIP5"],
        "mailing_fields": [],
        "echo_property_to_mailing": False,
        "skip_statewide_fallback": True,
        "out_fields": "PIN,ADDR_FULL,POSTALCTYNAME,STATE_ABBR,ZIP5",
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


def _format_kitsap(plain: str) -> list[str]:
    """Kitsap uses two dashed parcel formats, both 14 plain digits:
    - ``dddddd-d-ddd-dddd`` (6-1-3-4) e.g. ``012302-2-005-2007``
    - ``dddd-ddd-ddd-dddd`` (4-3-3-4) e.g. ``4178-000-002-0006``
    """
    if len(plain) == 14 and plain.isdigit():
        return [
            f"{plain[:6]}-{plain[6]}-{plain[7:10]}-{plain[10:]}",
            f"{plain[:4]}-{plain[4:7]}-{plain[7:10]}-{plain[10:]}",
        ]
    return []


# Per-county WA statewide parcel normalizers. Given the scraper's
# plain-digit parcel_id, returns a list of candidate ORIG_PARCEL_ID
# formats to try in the query. Empty list means fall back to plain
# digits (default behavior).
_WA_COUNTY_PARCEL_FORMATTERS: dict[str, callable] = {
    "kitsap": _format_kitsap,
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
    if gis_config and parcel_id:
        result = _query_gis(parcel_id, gis_config, county_key)
        if result.get("property_address"):
            return result
        # A parcel that MATCHED an authoritative county layer but has no street
        # (vacant/raw land) must not fall through to the statewide service, which
        # could return a wrong situs — mirror batch_enrich_parcels_gis
        # (skip_statewide_fallback). Return the matched-vacant result as-is.
        if result.get("matched") and gis_config.get("skip_statewide_fallback"):
            return result

    # Fallback: search by owner name if parcel ID didn't match or is None
    # (e.g. Tyler SelfService records that have names but no parcel)
    if gis_config and owner_name and gis_config.get("owner_field"):
        result = _query_gis_by_name(owner_name, gis_config, county_key)
        if result.get("property_address"):
            _logger.info("GIS name-based fallback succeeded for %s", owner_name)
            return result

    # Fallback: WA statewide parcel service (covers all 39 WA counties)
    if state.upper() == "WA" and parcel_id:
        result = _query_wa_statewide(parcel_id, county)
        if result.get("property_address"):
            return result

    # WA statewide name-based fallback when parcel_id is None
    if state.upper() == "WA" and not parcel_id and owner_name:
        result = _query_wa_statewide_by_name(owner_name, county)
        if result.get("property_address"):
            _logger.info("WA statewide name search succeeded for %s", owner_name)
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
        # SSRF-safe: endpoint comes from DB county_connectors.gis_endpoint.
        # safe_get validates (DNS-rebinding aware) + blocks private/metadata IPs.
        resp = safe_get(endpoint, params=params, require_allowlisted=False, timeout=10)

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
        # SSRF-safe: endpoint comes from DB county_connectors.gis_endpoint.
        # safe_get validates (DNS-rebinding aware) + blocks private/metadata IPs.
        resp = safe_get(endpoint, params=params, require_allowlisted=False, timeout=10)
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
        # S4: safe_http (SSRF defense-in-depth). Fixed HTTPS ArcGIS endpoint,
        # but safe_get re-validates (resolve=True), disables ambient proxy,
        # and refuses redirect-to-internal. Returns a requests.Response.
        resp = safe_get(_WA_STATEWIDE_ENDPOINT, params=params, timeout=15)

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
            # Clean newlines from addresses (WA statewide has embedded \n)
            address = " ".join(address.strip().split())
            if city:
                city = " ".join(city.strip().split())
            if zipcode:
                zipcode = str(zipcode).strip()

            # Build full mailing address
            parts = [address]
            if city:
                parts.append(city)
            if zipcode:
                parts.append(f"WA {zipcode}")
            mailing = ", ".join(parts)

            parcel_found = attrs.get("ORIG_PARCEL_ID") or apn_clean
            _logger.info("WA statewide GIS enriched parcel %s: %s", parcel_found, address)
            return {
                "property_address": address,
                "mailing_address": mailing,
                "parcel_id": parcel_found,
            }

        return _empty()

    except requests.exceptions.Timeout:
        _logger.warning("WA statewide GIS timed out for parcel %s", parcel_id)
        return _empty()
    except Exception as exc:
        _logger.warning("WA statewide GIS error for parcel %s: %s", parcel_id, str(exc)[:80])
        return _empty()


def _query_wa_statewide_by_name(owner_name: str, county: str) -> dict[str, str | None]:
    """WA statewide name-based fallback — intentionally a no-op.

    Referenced from enrich_parcel_gis() but the underlying WAGeoservices
    Current_Parcels FeatureServer publishes NO owner-name column (fields
    are FIPS_NR, COUNTY_NM, PARCEL_ID_NR, SITUS_ADDRESS, etc. — no
    OWNER_NM). A name search here is impossible against that service.

    Name-based enrichment is handled instead at the tasks.py layer via
    the connector's assessor_url (Tyler PACS PropertyAccess — see
    src/scrapers/enrichment/pacs.py). This stub exists so the
    enrich_parcel_gis() fallback branch does not raise NameError
    if called with parcel_id=None.
    """
    return _empty()


def _parse_gis_response(data: dict, gis_config: dict) -> dict[str, str | None]:
    """Parse ArcGIS REST API response into enrichment fields.

    Return keys: property_address, mailing_address, parcel_id, matched,
    vacant_no_situs, situs_city, situs_state, situs_zip. ``matched`` is True when
    the layer returned a feature for the parcel; ``vacant_no_situs`` is True when
    it matched but has NO street address (raw/vacant land). The caller keeps
    property_address NULL for a vacant parcel (skip-trace bills off it) but can
    surface the situs city/zip for display. A no-feature response is ``matched``
    False so the caller can still try a fallback source.
    """
    _NOMATCH = {
        "property_address": None, "mailing_address": None, "parcel_id": None,
        "matched": False, "vacant_no_situs": False,
        "situs_city": None, "situs_state": None, "situs_zip": None,
    }
    features = data.get("features") or []
    if not features:
        return _NOMATCH

    attrs = features[0].get("attributes") or {}

    # Situs city/state/zip from the ORDERED suffix fields [city, state, zip], when
    # configured. Completes a full mailable street address and, for a vacant/
    # unaddressed parcel, records WHERE it is for display.
    suffix = gis_config.get("address_suffix_fields") or []

    def _suffix(i: int) -> str | None:
        if i < len(suffix):
            v = attrs.get(suffix[i])
            return str(v).strip() if v and str(v).strip() else None
        return None

    situs_city, situs_state, situs_zip = _suffix(0), _suffix(1), _suffix(2)

    # Property/situs STREET address
    address_field = gis_config.get("address_field", "Site_Address")
    street = attrs.get(address_field) or None
    if street:
        street = str(street).replace("&nbsp;", "").strip() or None

    # Build the full "STREET, CITY, STATE ZIP" when suffix fields are configured
    # (better for export + skip-trace city/zip parsing than street-only).
    property_address = street
    if street and suffix:
        loc_parts = [p for p in (situs_city, situs_state) if p]
        city_state = ", ".join(loc_parts)
        if situs_zip:
            city_state = f"{city_state} {situs_zip}".strip()
        if city_state:
            property_address = f"{street}, {city_state}"

    # Mailing address. `mailing_fields` builds a real mailing; otherwise
    # echo_property_to_mailing (default True, back-compat) copies the property
    # address. King sets it False — no bulk taxpayer mailing exists, so mailing
    # stays NULL rather than a misleading property echo (Codex).
    mailing_fields = gis_config.get("mailing_fields", [])
    if mailing_fields:
        parts = []
        for field in mailing_fields:
            val = attrs.get(field)
            if val and str(val).strip():
                parts.append(str(val).strip())
        mailing_address = ", ".join(parts) if parts else None
    elif gis_config.get("echo_property_to_mailing", True):
        mailing_address = property_address
    else:
        mailing_address = None

    # Owner name (logged for enrichment_data, not in primary return)
    owner_field = gis_config.get("owner_field")
    owner_name = attrs.get(owner_field) if owner_field else None

    parcel_field = gis_config.get("parcel_field", "TaxParcelNumber")
    result = {
        "property_address": property_address,
        "mailing_address": mailing_address,
        "parcel_id": attrs.get(parcel_field) or None,
        "matched": True,
        "vacant_no_situs": not street,
        "situs_city": situs_city,
        "situs_state": situs_state,
        "situs_zip": situs_zip,
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


def batch_enrich_parcels_gis(
    parcel_ids: list[str], county: str, state: str
) -> dict[str, dict[str, str | None]]:
    """Batch enrich multiple parcels via GIS API.

    Strategy:
    1. Try county-specific endpoint first (has mailing address from Delivery_Address)
    2. Fall back to WA statewide for any parcels not found

    Processes in chunks of 50 (ArcGIS URL length limit).
    """
    if state.upper() != "WA":
        return {}

    results: dict[str, dict] = {}
    county_key = f"{county.lower()}_{state.upper()}"
    gis_config = _KNOWN_GIS_ENDPOINTS.get(county_key)

    # Step 1: County-specific endpoint (has real mailing addresses)
    if gis_config:
        results = _batch_query_county(parcel_ids, gis_config)

    # Step 2: WA statewide fallback ONLY for parcels the county endpoint did not
    # MATCH at all. A parcel that matched but has no street (vacant/raw land) is
    # already in `results` (matched=True, property_address=None) — it must NOT
    # fall through to statewide, which could return a wrong situs (Codex High).
    # `skip_statewide_fallback` disables the fallback for a county whose own layer
    # is authoritative (King): a dead PIN won't match statewide either, so the
    # fallback only risks false positives there.
    skip_statewide = bool(gis_config and gis_config.get("skip_statewide_fallback"))
    if not skip_statewide:
        missing = [pid for pid in parcel_ids if pid not in results and pid and len(pid.strip()) >= 6]
        if missing:
            statewide = _batch_query_wa_statewide(missing, county)
            results.update(statewide)

    return results


def _batch_query_county(
    parcel_ids: list[str], gis_config: dict
) -> dict[str, dict[str, str | None]]:
    """Batch query a county-specific ArcGIS endpoint (has mailing address)."""
    endpoint = gis_config["endpoint"]
    parcel_field = gis_config["parcel_field"]
    out_fields = gis_config.get("out_fields", "*")
    results: dict[str, dict] = {}
    chunk_size = 50

    for i in range(0, len(parcel_ids), chunk_size):
        chunk = parcel_ids[i:i + chunk_size]
        clean = [pid.replace("-", "").strip() for pid in chunk if pid and len(pid.strip()) >= 6]
        if not clean:
            continue

        in_clause = ",".join(f"'{p}'" for p in clean)
        params = {
            "where": f"{parcel_field} IN ({in_clause})",
            "outFields": out_fields,
            "returnGeometry": "false",
            "f": "json",
            "resultRecordCount": chunk_size,
        }

        try:
            # SSRF-safe (DB-supplied endpoint) — see _query_gis.
            resp = safe_get(endpoint, params=params, require_allowlisted=False, timeout=30)
            if resp.status_code != 200:
                _logger.warning("County GIS batch returned %d", resp.status_code)
                continue

            data = resp.json()
            for feature in data.get("features") or []:
                attrs = feature.get("attributes") or {}
                pid = attrs.get(parcel_field)
                if not pid:
                    continue

                parsed = _parse_gis_response({"features": [feature]}, gis_config)
                # Keep EVERY matched parcel — including a vacant/raw-land parcel
                # whose ADDR_FULL is null (property_address=None, matched=True) —
                # so batch_enrich_parcels_gis does NOT send it to the statewide
                # fallback and pick up a wrong situs address (Codex High). The
                # consumer surfaces the situs city/zip for these from enrichment_data.
                parsed["parcel_id"] = pid
                results[pid] = parsed

            _addr = len([p for p in clean if results.get(p, {}).get("property_address")])
            _logger.info(
                "County GIS batch: %d/%d matched, %d with street address",
                len([p for p in clean if p in results]), len(clean), _addr,
            )

        except Exception as exc:
            _logger.warning("County GIS batch error: %s", str(exc)[:80])

    return results


def _batch_query_wa_statewide(
    parcel_ids: list[str], county: str
) -> dict[str, dict[str, str | None]]:
    """Batch query WA statewide endpoint (property address only, no mailing).

    Some counties (Kitsap) store parcels with embedded dashes in
    ORIG_PARCEL_ID. The per-county formatter in
    ``_WA_COUNTY_PARCEL_FORMATTERS`` converts our scraper's plain-digit
    parcel into the canonical county format before querying, then we
    map results back to the caller's original parcel_id.
    """
    fips = _WA_COUNTY_FIPS.get(county.lower())
    formatter = _WA_COUNTY_PARCEL_FORMATTERS.get(county.lower())
    results: dict[str, dict] = {}
    chunk_size = 50

    for i in range(0, len(parcel_ids), chunk_size):
        chunk = parcel_ids[i:i + chunk_size]

        # Build (query_value, original_parcel_id) pairs so we can map
        # responses back to the scraper's parcel_id regardless of format.
        # Formatters may return multiple candidate formats (e.g., Kitsap
        # uses two different dash patterns) — we try them all.
        query_pairs: list[tuple[str, str]] = []
        for pid in chunk:
            if not pid or len(pid.strip()) < 6:
                continue
            plain = pid.replace("-", "").strip()
            if formatter:
                candidates = formatter(plain)
                if candidates:
                    for c in candidates:
                        query_pairs.append((c, pid))
                    continue
            query_pairs.append((plain, pid))

        if not query_pairs:
            continue

        # Reverse map: query_value -> original caller parcel_id
        query_to_original = dict(query_pairs)
        in_values = list(query_to_original.keys())

        in_clause = ",".join(f"'{p}'" for p in in_values)
        where = f"ORIG_PARCEL_ID IN ({in_clause})"
        if fips:
            where += f" AND FIPS_NR='{fips}'"

        params = {
            "where": where,
            "outFields": "ORIG_PARCEL_ID,SITUS_ADDRESS,SITUS_CITY_NM,SITUS_ZIP_NR",
            "returnGeometry": "false",
            "f": "json",
            "resultRecordCount": chunk_size,
        }

        try:
            # S4: safe_http (SSRF defense-in-depth) — see batch note above.
            resp = safe_get(_WA_STATEWIDE_ENDPOINT, params=params, timeout=30)
            if resp.status_code != 200:
                continue

            data = resp.json()
            for feature in data.get("features") or []:
                attrs = feature.get("attributes") or {}
                pid = attrs.get("ORIG_PARCEL_ID")
                address = attrs.get("SITUS_ADDRESS")
                if not pid or not address:
                    continue

                # Map back to caller's original parcel_id
                caller_pid = query_to_original.get(pid, pid)

                address = " ".join(address.strip().split())
                city = (attrs.get("SITUS_CITY_NM") or "").strip()
                zipcode = (attrs.get("SITUS_ZIP_NR") or "").strip()
                # Build mailing from situs address + city + state + zip
                mailing = address
                if city:
                    mailing += f", {city}"
                mailing += ", WA"
                if zipcode:
                    mailing += f" {zipcode}"
                results[caller_pid] = {
                    "property_address": address,
                    "mailing_address": mailing,
                    "parcel_id": pid,  # canonical format from server
                }

            _logger.info(
                "Statewide GIS batch: %d/%d parcels enriched",
                len([qv for qv, orig in query_pairs if orig in results]),
                len(query_pairs),
            )

        except Exception as exc:
            _logger.warning("Statewide GIS batch error: %s", str(exc)[:80])

    return results
