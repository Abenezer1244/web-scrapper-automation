"""Free county GIS REST API enrichment.

Most US counties run ArcGIS-based GIS portals with free, unauthenticated
REST APIs. This module queries those endpoints for parcel data.

Cost: $0 — no API key, no rate limits (be polite though).

Each county's GIS endpoint URL is stored in county_connectors.gis_endpoint.
The ArcGIS REST query format is standardized across all counties.
"""

import re

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
    # Snohomish County — public hosted parcel layer. Before this entry Snohomish had
    # NO mailing source at all: it fell through to the WA statewide situs-only layer,
    # so property_address filled and mailing_address was structurally always NULL
    # (0/30 live pre_foreclosure + trustee_sale leads, 2026-09-10).
    #
    # `taxpr*` is the TAXPAYER block (who the tax bill is mailed to) — the same thing
    # King's tax-bill scrape reads. `owner*` is a separate block on this layer; taxpayer
    # is the mailing-of-record and is what the other counties store.
    #
    # taxprline2/3 are deliberately NOT read: line3 is empty layer-wide and line2 is
    # populated on 6 parcels out of ~250k, half of them "C/O <person>" — an addressee
    # NAME, which this codebase does not collect. taxprname is a name for the same
    # reason. parcel_id is a 14-char string with leading zeros; NEVER int-cast it.
    "snohomish_WA": {
        "endpoint": (
            "https://gis.snoco.org/host/rest/services"
            "/Hosted/CADASTRAL__parcels/FeatureServer/0/query"
        ),
        "parcel_field": "parcel_id",
        "address_field": "situsline1",
        # Structured situs only — NOT address_suffix_fields, which would fold city/
        # state/zip into property_address itself. These counties reach here from the
        # statewide layer today, which stores a street-only property_address plus
        # separate property_city/state/zip; keep that shape byte-for-byte so adding
        # a mailing source does not silently reformat every existing lead's address.
        "situs_part_fields": ["situscity", "situsstate", "situszip"],
        "mailing_street_fields": ["taxprline1"],
        "mailing_locality_fields": ["taxprcity", "taxprstate", "taxprzip"],
        "out_fields": (
            "parcel_id,situsline1,situscity,situsstate,situszip,"
            "taxprline1,taxprcity,taxprstate,taxprzip"
        ),
        # LICENSE: the county open-data terms for this parcel dataset say users "will
        # not use any lists of individuals, or data from which such lists may be
        # compiled, for any commercial purpose" (RCW 42.56.070(8)). The taxpayer
        # mailing block stays OFF until counsel clears it; see _effective_gis_config.
        "mailing_license_restricted": True,
        "situs_only_out_fields": "parcel_id,situsline1,situscity,situsstate,situszip",
    },
    # Cowlitz County — Assessor parcel layer. Same story as Snohomish: no mailing
    # source before this entry (0/22 live probate leads, 2026-09-10).
    #
    # Two shapes this layer needs that no other config had:
    #   * situs is SPLIT across five columns, so `address_part_fields` composes it;
    #   * the mailing street is in DEED_HOLDER_ADDRESS_2 for 53,109 parcels while
    #     _ADDRESS_1 holds an "ATTN …"/"C/O …" addressee. _2 is therefore tried first
    #     and _1 only when it independently looks like a street (203 such parcels).
    #
    # Use Assessor/Parcels, NOT Assessor/Cowlitz_Tax_Parcels — the latter shares
    # DEED_HOLDER_NAME but publishes no mailing columns at all. PARCNO is a string and
    # carries leading zeros ("08931001"); verified 2026-09-10 that none contain dashes.
    "cowlitz_WA": {
        "endpoint": (
            "https://gis.cowlitzwa.gov/ccserver/rest/services"
            "/Assessor/Parcels/MapServer/0/query"
        ),
        "parcel_field": "PARCNO",
        "address_part_fields": [
            "SITUS_STREET_NUMBER", "SITUS_STREET_DIRECTION", "SITUS_STREET_NAME",
            "SITUS_STREET_SUFFIX", "SITUS_STREET_UNIT",
        ],
        # No SITUS_STATE column exists on this layer; the state is WA by construction
        # (it is the Cowlitz County assessor's own service), the same reasoning
        # _situs_parts applies to the WA statewide layer.
        "situs_part_fields": ["SITUS_CITY", None, "SITUS_ZIP_CODE"],
        "situs_state_literal": "WA",
        "mailing_street_fields": ["DEED_HOLDER_ADDRESS_2", "DEED_HOLDER_ADDRESS_1"],
        "mailing_locality_fields": [
            "DEED_HOLDER_CITY", "DEED_HOLDER_STATE", "DEED_HOLDER_ZIPCODE",
        ],
        "out_fields": (
            "PARCNO,SITUS_STREET_NUMBER,SITUS_STREET_DIRECTION,SITUS_STREET_NAME,"
            "SITUS_STREET_SUFFIX,SITUS_STREET_UNIT,SITUS_CITY,SITUS_ZIP_CODE,"
            "DEED_HOLDER_ADDRESS_1,DEED_HOLDER_ADDRESS_2,DEED_HOLDER_CITY,"
            "DEED_HOLDER_STATE,DEED_HOLDER_ZIPCODE"
        ),
        # Owner (deed holder) mailing held OFF with Snohomish pending legal review of
        # commercial use (owner decision 2026-09-13); see _effective_gis_config.
        "mailing_license_restricted": True,
        "situs_only_out_fields": (
            "PARCNO,SITUS_STREET_NUMBER,SITUS_STREET_DIRECTION,SITUS_STREET_NAME,"
            "SITUS_STREET_SUFFIX,SITUS_STREET_UNIT,SITUS_CITY,SITUS_ZIP_CODE"
        ),
    },
}

_MAILING_KEYS = ("mailing_street_fields", "mailing_locality_fields", "mailing_fields")


def _effective_gis_config(county_key: str) -> dict | None:
    """The county GIS config actually used, with license-restricted mailing removed.

    A config marked ``mailing_license_restricted`` keeps its situs (property address)
    lookup but loses every mailing field, and requests only situs columns, unless
    ``settings.COUNTY_GIS_RESTRICTED_MAILING_ENABLED`` is on. So while it is off, no
    owner or taxpayer mailing data is requested, stored, or offered as a source.
    Every lookup of _KNOWN_GIS_ENDPOINTS goes through here.
    """
    cfg = _KNOWN_GIS_ENDPOINTS.get(county_key)
    if not cfg or not cfg.get("mailing_license_restricted"):
        return cfg
    if settings.COUNTY_GIS_RESTRICTED_MAILING_ENABLED:
        return cfg
    gated = {k: v for k, v in cfg.items() if k not in _MAILING_KEYS}
    gated["out_fields"] = cfg["situs_only_out_fields"]
    return gated


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
        # An explicit endpoint override must not reopen a license-restricted
        # county's mailing: drop every mailing field so none is parsed or stored.
        # (out_fields is left as the override's own list; its column names are
        # not guaranteed to match the known layer's situs fields.)
        known = _KNOWN_GIS_ENDPOINTS.get(county_key) or {}
        if known.get("mailing_license_restricted") and not settings.COUNTY_GIS_RESTRICTED_MAILING_ENABLED:
            gis_config = {k: v for k, v in gis_config.items() if k not in _MAILING_KEYS}
    elif county_key in _KNOWN_GIS_ENDPOINTS:
        gis_config = _effective_gis_config(county_key)

    # Try county-specific endpoint first (by parcel ID)
    county_mailing: str | None = None
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
        # The county knew where the owner gets mail but not where the property is.
        # Every fallback below is situs-only, so returning one of them wholesale
        # would discard that mailing address — the same loss the batch path was
        # fixed for (Codex P1, 2026-09-10). Carry it forward instead.
        county_mailing = result.get("mailing_address")

    # Fallback: WA statewide parcel service (covers all 39 WA counties).
    #
    # ORDER MATTERS: this EXACT parcel lookup must run before either name-based
    # fallback. A name search reduces the owner to its first token and asks for
    # ONE row, so it can only ever return "some parcel owned by someone whose
    # surname starts like this" — for a common surname that is a DIFFERENT
    # property. Letting it pre-empt an exact APN match means silently attaching
    # the wrong address to a lead. Exact identifier always beats fuzzy name
    # (Codex, 2026-09-03).
    if state.upper() == "WA" and parcel_id:
        result = _query_wa_statewide(parcel_id, county)
        if result.get("property_address"):
            if county_mailing and not result.get("mailing_address"):
                # Same parcel id, so the county's mailing address still describes
                # this property's owner. The statewide layer has none to offer.
                result = {**result, "mailing_address": county_mailing}
            return result

    # Fallback: search by owner name. Skipped when a parcel id is in hand AND an
    # exact statewide lookup already had its chance at it — today that means WA,
    # whose Current_Parcels layer covers all 39 counties. There, a surname guess
    # is never an acceptable substitute for the exact APN paths above.
    #
    # Deliberately NOT a blanket `not parcel_id`: outside WA there is no
    # statewide exact service, so a county parcel miss leaves this as the ONLY
    # remaining fallback, and banning it globally would silently kill enrichment
    # for every non-WA gis_endpoint config whose parcel id is merely stale or
    # mis-parsed. Narrowing the ban to the states that HAVE an exact fallback
    # removes the wrong-property hazard without creating a dead path (Codex).
    _has_exact_statewide_fallback = state.upper() == "WA"
    _name_search_preempts_exact = bool(parcel_id) and _has_exact_statewide_fallback
    if (
        gis_config
        and owner_name
        and not _name_search_preempts_exact
        and gis_config.get("owner_field")
    ):
        result = _query_gis_by_name(owner_name, gis_config, county_key)
        if result.get("property_address"):
            # PII: never log owner_name — these are third parties who never signed
            # up, and the log file has no rotation or retention. county_key keeps the
            # operational signal ("the name fallback worked here") without the person.
            # Same reasoning as tasks_helpers/enrich.py's skip-trace logging.
            _logger.info("GIS name-based fallback succeeded for %s", county_key)
            return result

    # WA statewide name-based fallback when parcel_id is None
    if state.upper() == "WA" and not parcel_id and owner_name:
        result = _query_wa_statewide_by_name(owner_name, county)
        if result.get("property_address"):
            # PII: county, not owner_name (see the note on the fallback above).
            _logger.info("WA statewide name search succeeded for %s", county)
            return result

    # Nothing located the property. The county's mailing address is still a real
    # answer for the parcel that was asked about, so it is returned rather than
    # thrown away with the miss. The two name-search returns above deliberately do
    # NOT carry it: they may have landed on a DIFFERENT parcel, and this mailing
    # address belongs to the one identified by parcel_id.
    if county_mailing:
        return {**_empty(), "mailing_address": county_mailing}
    return _empty()


def _query_gis(parcel_id: str, gis_config: dict, county_key: str) -> dict[str, str | None]:
    """Query a county-specific ArcGIS REST endpoint."""
    endpoint = gis_config["endpoint"]
    parcel_field = gis_config["parcel_field"]
    out_fields = gis_config.get("out_fields", "*")

    # Strip dashes from parcel ID (some sources include them)
    apn_clean = parcel_id.replace("-", "").strip()

    params = {
        "where": f"{parcel_field}={_arcgis_literal(apn_clean)}",
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
    # A LIKE metacharacter in the name would silently widen the match to a
    # different owner's parcel. Not a legitimate character in a surname, so
    # reject rather than rewrite the name — same rule, same reason, as
    # pierce_legal_repair._LIKE_META (Codex, 2026-09-03).
    if _LIKE_META.search(name_clean):
        _logger.warning(
            "GIS owner-name lookup skipped: LIKE metacharacter in %r", name_clean[:40]
        )
        return _empty()

    params = {
        # Quote-escaped: an apostrophe is ORDINARY in a surname (O'BRIEN,
        # O'CONNOR, D'ANGELO) and raw interpolation made the predicate
        # malformed — ArcGIS errored, the except below swallowed it, and those
        # owners silently got NO enrichment at all. The trailing % is appended
        # INSIDE the literal so it stays a wildcard after escaping.
        "where": f"{owner_field} LIKE {_arcgis_literal(name_clean + '%')}",
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
        # PII: county_key, not owner_name — the exception text carries the diagnosis.
        _logger.warning("GIS name search error for %s: %s", county_key, str(exc)[:60])
        return _empty()


def _query_wa_statewide(parcel_id: str, county: str) -> dict[str, str | None]:
    """Query the WA statewide parcel service (covers all 39 WA counties).

    Endpoint: WAGeoservices Current_Parcels FeatureServer
    Fields: ORIG_PARCEL_ID, SITUS_ADDRESS, SITUS_CITY_NM, SITUS_ZIP_NR
    Filter: FIPS_NR for county scoping.
    """
    apn_clean = parcel_id.replace("-", "").strip()
    fips = _WA_COUNTY_FIPS.get(county.lower())

    where_clause = f"ORIG_PARCEL_ID={_arcgis_literal(apn_clean)}"
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

            parcel_found = attrs.get("ORIG_PARCEL_ID") or apn_clean
            _logger.info("WA statewide GIS enriched parcel %s: %s", parcel_found, address)
            return {
                "property_address": address,
                # The statewide layer is SITUS-only: it never knows where the owner
                # gets mail, so it never sets one (2026-09-02 policy: no assumed
                # owner-occupancy — "real data everywhere").
                "mailing_address": None,
                "parcel_id": parcel_found,
                **_situs_parts(city, zipcode),
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
    if gis_config.get("address_part_fields"):
        # Layers that publish the situs street in components (house number,
        # prefix, name, suffix, unit) rather than one column.
        street = _compose_street_parts(attrs, gis_config["address_part_fields"])
    else:
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

    # Mailing address (may be multiple fields joined).
    #
    # #153 arrived with an `echo_property_to_mailing` switch defaulting to TRUE
    # for back-compat, and King setting it False. That switch predates #188: main
    # has since removed the echo ENTIRELY, because copying the situs into
    # mailing_address states an owner-occupancy fact no GIS layer gave us
    # (2026-09-02 policy). Merging #153's default back in would have silently
    # restored the echo for every county EXCEPT King — the opposite of the intent.
    # The stricter rule wins; King's `echo_property_to_mailing: False` is now
    # simply redundant, and harmless.
    mailing_fields = gis_config.get("mailing_fields", [])
    if gis_config.get("mailing_street_fields"):
        # Layers whose mailing block is street + separate city/state/zip, and whose
        # street may live in one of several columns. Formatted "STREET, CITY, ST ZIP"
        # instead of the flat join below, which would emit "…, WA, 98043".
        mailing_address = _compose_mailing(attrs, gis_config)
    elif mailing_fields:
        parts = []
        for field in mailing_fields:
            val = attrs.get(field)
            if val and str(val).strip():
                parts.append(str(val).strip())
        mailing_address = ", ".join(parts) if parts else None
    else:
        # A GIS config with no mailing fields knows nothing about the owner's
        # mail — never copy the situs in as if it did (2026-09-02 policy).
        mailing_address = None

    # Owner name (logged for enrichment_data, not in primary return)
    owner_field = gis_config.get("owner_field")
    owner_name = attrs.get(owner_field) if owner_field else None

    parcel_field = gis_config.get("parcel_field", "TaxParcelNumber")
    result = {
        "property_address": property_address,
        "mailing_address": mailing_address,
        "parcel_id": attrs.get(parcel_field) or None,
        # 085 structured situs (#188) and the match/vacant signals (#153) are
        # disjoint key sets and BOTH are consumed downstream — keep both.
        **(_situs_parts_direct(attrs, gis_config, has_street=bool(street))
           or _situs_parts_from_confirmed_mailing(attrs, property_address, gis_config)),
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


# Counties whose owner mailing comes from a BULK county export rather than a live
# per-parcel layer. Registered explicitly, NOT inferred from the GIS config: the
# Snohomish GIS mailing fields are dead weight now, and deleting them must not also
# delete Snohomish from mailing recovery (Codex).
_BULK_MAILING_COUNTIES: frozenset[str] = frozenset({"snohomish_WA"})

# Bulk exports carrying the SAME RCW 42.56.070(8) restriction on lists of
# individuals as the county's live layer. The Snohomish Assessor Roll is literally
# the same taxpayer block behind the same clause, so it MUST answer to the same
# kill switch: gating only the live layer would mean turning the switch off for a
# legal reason silently stopped nothing.
_BULK_MAILING_LICENSE_RESTRICTED: frozenset[str] = frozenset({"snohomish_WA"})


def has_bulk_mailing_source(county: str, state: str) -> bool:
    """True when a county publishes owner mailing as a bulk file we can resolve."""
    key = f"{(county or '').lower()}_{(state or '').upper()}"
    if key not in _BULK_MAILING_COUNTIES:
        return False
    if (key in _BULK_MAILING_LICENSE_RESTRICTED
            and not settings.COUNTY_GIS_RESTRICTED_MAILING_ENABLED):
        return False
    return True


def _resolve_bulk_mailing(county_key: str, parcel_ids: list[str]) -> dict:
    """Bulk-source answers for these parcels, keyed by caller id. Never raises."""
    if county_key != "snohomish_WA":
        return {}
    try:
        from src.scrapers.enrichment.snohomish_assessor_roll import resolve_mailing

        return resolve_mailing(parcel_ids)
    except Exception as exc:  # noqa: BLE001 -- enrichment is best-effort
        _logger.warning("Bulk mailing source failed for %s: %s", county_key, str(exc)[:160])
        return {}


def has_gis_mailing_source(county: str, state: str) -> bool:
    """True when this county's own GIS layer publishes the owner's mailing address.

    King is deliberately False: its public layer withholds the taxpayer block and its
    mailing comes from the per-parcel eRealProperty pass instead.
    """
    cfg = _effective_gis_config(f"{(county or '').lower()}_{(state or '').upper()}") or {}
    return bool(cfg.get("mailing_street_fields") or cfg.get("mailing_fields"))


def gis_mailing_source_counties(state: str = "WA") -> list[str]:
    """Lower-case county names with ANY mailing source — live layer or bulk export.

    Background recovery selects its candidates from this list, so a county served
    only by a bulk file has to appear here or its deferred rows are never retried.
    """
    suffix = f"_{state.upper()}"
    keys = set(_KNOWN_GIS_ENDPOINTS) | _BULK_MAILING_COUNTIES
    return sorted(
        key[: -len(suffix)] for key in keys
        if key.endswith(suffix)
        and (has_gis_mailing_source(key[: -len(suffix)], state)
             or has_bulk_mailing_source(key[: -len(suffix)], state))
    )


def batch_enrich_parcels_gis(
    parcel_ids: list[str], county: str, state: str, stats: dict | None = None
) -> dict[str, dict[str, str | None]]:
    """Batch enrich multiple parcels via GIS API.

    Strategy:
    1. Try county-specific endpoint first (has mailing address from Delivery_Address)
    2. Fall back to WA statewide for any parcels not found

    Processes in chunks of 50 (ArcGIS URL length limit).

    ``stats`` (optional out-param): ``county_unreached`` lists the caller parcel ids
    whose county request did not produce a usable answer (non-200, exception, or an
    ArcGIS error body). For a county with a mailing source those parcels were never
    looked up, which is different from "looked up, no mailing address", so the caller
    can defer them to background recovery instead of leaving a silent NULL.
    """
    if stats is not None:
        stats.setdefault("county_unreached", [])
    if state.upper() != "WA":
        return {}

    results: dict[str, dict] = {}
    county_key = f"{county.lower()}_{state.upper()}"
    gis_config = _effective_gis_config(county_key)

    # Step 1: County-specific endpoint (has real mailing addresses)
    if gis_config:
        results = _batch_query_county(
            parcel_ids, gis_config,
            unreached=stats["county_unreached"] if stats is not None else None,
        )

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
        # Rows the county answered with a mailing address but no situs street. They
        # are already in `results`, so the `missing` filter above skips them; they
        # still need the statewide layer for the property address. A plain
        # `results.update()` would replace the county row wholesale and drop the
        # mailing address we came here for, so these merge field-by-field instead.
        needs_situs = [
            pid for pid in parcel_ids
            if pid in results and results[pid].get("needs_situs_fallback")
        ]
        if missing or needs_situs:
            statewide = _batch_query_wa_statewide(missing + needs_situs, county)
            for pid, sw_row in statewide.items():
                county_row = results.get(pid)
                if county_row and county_row.get("needs_situs_fallback"):
                    merged = dict(sw_row)
                    # County mailing wins; the statewide layer has none to offer.
                    merged["mailing_address"] = (
                        county_row.get("mailing_address") or sw_row.get("mailing_address")
                    )
                    # Same parcel, so the county's own situs parts stay valid wherever
                    # the statewide row has none (Codex P2): replacing the row wholesale
                    # dropped a city/ZIP the county had already published.
                    for key in ("property_city", "property_state", "property_zip"):
                        if not merged.get(key) and county_row.get(key):
                            merged[key] = county_row[key]
                    results[pid] = merged
                else:
                    results[pid] = sw_row

    for row in results.values():
        row.pop("needs_situs_fallback", None)

    # ─── Bulk mailing source ────────────────────────────────────────────────
    # Runs LAST, after the statewide fallback has settled every property address,
    # so filling a mailing-only answer can never remove a parcel from the situs
    # `missing` set and cost it its property address (Codex High).
    #
    # Eligibility is "still has no mailing", NOT "was reported unreached": an empty
    # FEATURE SET, or a future revision that keeps situs and drops only the taxpayer
    # block, leaves a parcel with no mailing and no unreached marker, and those are
    # exactly the parcels this source exists to answer (Codex).
    if has_bulk_mailing_source(county, state):
        needs_mail = [
            pid for pid in dict.fromkeys(parcel_ids)
            if pid and not (results.get(pid) or {}).get("mailing_address")
        ]
        if needs_mail:
            answers = _resolve_bulk_mailing(county_key, needs_mail)
            filled_pids: set[str] = set()
            for pid, answer in answers.items():
                if not getattr(answer, "is_found", False):
                    # NOT an answer about this parcel. Leaving it unmarked is what
                    # made this a NO-GO: with the county layer dead, a parcel whose
                    # feature set came back EMPTY carries no unreached marker either,
                    # so recovery would read the pair as "attempted, no mailing
                    # address", write a terminal `none` and clear the deferral
                    # permanently — on a momentary download failure (Codex High).
                    # Every non-found outcome therefore stays deferrable:
                    #   source_unavailable — we never got to ask
                    #   ambiguous          — two taxpayer rows disagree; a later
                    #                        revision may settle it
                    #   absent_in_snapshot — no association in THIS revision, which
                    #                        is not the same as "has no mailing
                    #                        address" (Codex)
                    # _rotate charges no attempt, so these cycle rather than burn
                    # the ceiling. A revision-scoped terminal state would let the
                    # genuinely-absent ones settle; that is deliberate future work,
                    # noted because the queue for this county is small (tens of rows).
                    continue
                row = dict(results.get(pid) or _empty())
                row["mailing_address"] = answer.mailing_address
                row["mailing_source"] = "snohomish_assessor_roll"
                row["mailing_role"] = answer.role
                row["mailing_revision"] = answer.revision
                results[pid] = row
                filled_pids.add(pid)
                # It has been answered, so it must not also be reported as a parcel
                # we failed to reach — that would defer a row we just filled.
                if stats is not None and pid in stats.get("county_unreached", []):
                    stats["county_unreached"].remove(pid)

            # Derived from what was ASKED, not from what came BACK. _resolve_bulk_mailing
            # swallows its exceptions and returns {}, so iterating the response left
            # every parcel unmarked on exactly the failure the deferral exists for,
            # reopening the High it was meant to close (Codex).
            unresolved = [pid for pid in needs_mail if pid not in filled_pids]
            if unresolved and stats is not None:
                _note_unreached(stats["county_unreached"],
                                {pid: [pid] for pid in unresolved})
            if filled_pids or unresolved:
                _logger.info(
                    "Bulk mailing source: filled %d, deferred %d of %d %s parcels",
                    len(filled_pids), len(unresolved), len(needs_mail), county.lower(),
                )

    return results


def _arcgis_literal(value: str) -> str:
    """Quote a value for an ArcGIS ``where`` clause (SQL-92 style: '' escapes ').

    Parcel ids come from scraper regexes, but this is app-wide enrichment — a
    stray quote in a parsed value must not break or reshape the predicate (Codex).
    """
    return "'" + value.replace("'", "''") + "'"


def _callers_for(pid: object, clean_to_originals: dict[str, list[str]]) -> list[str]:
    """Caller parcel ids a returned feature is allowed to answer for.

    The query is an exact ``IN`` over values we supplied, but nothing forced the
    server to echo them verbatim: an ArcGIS layer typed numeric, or a JSON decoder,
    can hand back ``8931001`` for the ``08931001`` we asked about. Matching only on
    the raw string silently misses those, and the previous ``or [str(pid)]`` fallback
    did something worse — it invented a caller id nobody requested and filed a real
    owner's mailing address under it.

    Match on the raw string first, then on a dash- and leading-zero-insensitive form.
    The county path keys its requests dashless while the statewide path can key them
    dashed (Kitsap), so a layer echoing "602543-087-0" for "6025430870", or the
    reverse, still resolves (Codex P2). A feature that still corresponds to no
    requested id is dropped, never guessed at.
    """
    raw = str(pid).strip()
    exact = clean_to_originals.get(raw)
    if exact:
        return exact

    def _loose(value: str) -> str:
        return value.replace("-", "").strip().lstrip("0")

    loose = _loose(raw)
    if loose:
        # Distinct caller lists, not keys: Kitsap queries two dashed spellings of ONE
        # caller's parcel, and those must not read as two competing parcels.
        hits: list[list[str]] = []
        for clean, originals in clean_to_originals.items():
            if _loose(clean) == loose and originals not in hits:
                hits.append(originals)
        # Only when exactly ONE requested parcel collapses to this form. If a chunk
        # holds both "0123456" and "123456" they are different parcels that share a
        # loose key, and picking either would put one owner's mailing address on the
        # other's property. Ambiguity resolves to "drop", never to a guess.
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            _logger.warning(
                "County GIS parcel %r is ambiguous across %d requested ids, dropped",
                raw, len(hits),
            )
            return []
    _logger.warning(
        "County GIS returned parcel %r that matches no requested id, dropped", raw
    )
    return []


# Config keys naming the RAW attribute columns a layer publishes data in. Used to
# decide whether a returned feature carries any payload at all. Deliberately EXCLUDES
# parcel_field (an id, always present on a match), geometry, dates and
# situs_state_literal (a constant we supply, not something the county answered).
_PAYLOAD_FIELD_KEYS = (
    "address_field",
    "address_suffix_fields",
    "address_part_fields",
    "situs_part_fields",
    "mailing_fields",
    "mailing_street_fields",
    "mailing_locality_fields",
)


def _configured_payload_fields(gis_config: dict) -> list[str]:
    """Every raw column this config reads data (not identity) out of."""
    fields: list[str] = []
    for key in _PAYLOAD_FIELD_KEYS:
        value = gis_config.get(key)
        if isinstance(value, str):
            fields.append(value)
        elif isinstance(value, (list, tuple)):
            # situs_part_fields uses None as a positional placeholder (Cowlitz has
            # no SITUS_STATE column); those positions name no column to read.
            fields.extend([f for f in value if isinstance(f, str) and f])
    return fields


def _config_has_mailing_source(gis_config: dict) -> bool:
    """Does this (EFFECTIVE) config actually read a mailing address anywhere?

    The degraded marker exists to send a row to mailing recovery, so it is only
    meaningful where a mailing source exists. Gating on it keeps King out: King
    declares ``mailing_fields: []`` and ``skip_statewide_fallback``, and ~1/3 of its
    delinquent parcels are vacant/raw land that legitimately return NO situs at all.
    Classifying those as degraded would drop the feature instead of returning
    matched + vacant_no_situs, losing the vacant marker the worker persists and
    letting property recovery queue lookups for land that has no address to find
    (Codex High/Medium). A license-gated config whose mailing fields have been
    stripped also lands here and correctly keeps its pre-existing behaviour.
    """
    return bool(gis_config.get("mailing_fields") or gis_config.get("mailing_street_fields"))


def _feature_payload_is_empty(attrs: dict, gis_config: dict) -> bool:
    """The layer matched this parcel but answered with NO data at all.

    Snohomish stripped every attribute off its public parcel layer between
    2026-09-14 and 2026-09-18: 0 of 319,733 rows kept a non-null situsline1,
    ownername, taxprname, mkttl or usecode, while the service still returned HTTP
    200 and still matched on parcel_id. Nothing upstream could tell that apart from
    "this parcel genuinely has no mailing address", so every Snohomish lead was
    finalised with mailing_address NULL and nothing ever asked again — a source that
    went from 94% coverage to 0% produced no signal whatsoever.

    An identifier-only feature is INDETERMINATE, not an authoritative negative: it
    is routed to ``county_unreached`` so the row defers to background recovery the
    same way a timeout does. It is deliberately NOT a source-wide verdict — one
    parcel proves nothing about a county, and a genuinely data-less parcel is
    observationally identical (Codex). Declaring the whole layer down is the
    canary's job, not this predicate's.

    A vacant/raw-land parcel that really has no situs but DOES carry a taxpayer
    mailing address fails this predicate (its mailing column is populated) and is
    still treated as a real answer.
    """
    fields = _configured_payload_fields(gis_config)
    if not fields:
        # A config that reads no data columns (identity-only) can never be judged
        # empty — there is nothing it was supposed to return.
        return False
    return all(not str(attrs.get(f) or "").strip() for f in fields)


def _map_county_features(
    features: list[dict],
    gis_config: dict,
    clean_to_originals: dict[str, list[str]],
    degraded: list[str] | None = None,
) -> dict[str, dict[str, str | None]]:
    """Map county-GIS features onto the CALLER's parcel ids.

    ``degraded`` (optional out-param): caller parcel ids whose feature came back
    carrying no attribute payload at all. The caller folds these into
    ``county_unreached`` so they defer to recovery instead of reading as an
    authoritative "this parcel has no mailing address".

    The query strips dashes ("602543-087-0" -> "6025430870") and the server echoes
    the canonical form back, but the worker applies results by the lead's RAW
    parcel_id (tasks_helpers/enrich.py keys its rows on ``res.parcel_id``). Keying
    the dict by the server value meant every dashed parcel — 11 of 33 live Pierce
    NTS notices print them that way (2026-09-02) — got NO county data and fell
    through to the situs-only statewide service, losing the real mailing address.
    Mirror the statewide path: key by the original id(s). One APN can arrive under
    several raw spellings in a batch ("602543-087-0" and "6025430870"), so the
    feature fans out to EVERY caller id that collapsed to it (Codex). ``parcel_id``
    in each value stays the server's canonical form. Pure (no I/O) so the mapping
    is unit-tested against a real ArcGIS feature.
    """
    parcel_field = gis_config["parcel_field"]
    results: dict[str, dict] = {}
    for feature in features:
        attrs = feature.get("attributes") or {}
        pid = attrs.get(parcel_field)
        if not pid:
            continue
        if _config_has_mailing_source(gis_config) and _feature_payload_is_empty(attrs, gis_config):
            # Matched, but the layer returned an identifier and nothing else. Do not
            # let this reach _parse_gis_response: for a county with its own
            # authoritative layer that would come back matched + vacant_no_situs and
            # be PERSISTED as raw land (Codex), and for a fallback county it would be
            # dropped here and read as a settled negative. Record it as unreached and
            # leave the parcel to the statewide layer for its property address.
            if degraded is not None:
                for caller_pid in _callers_for(pid, clean_to_originals):
                    if caller_pid not in degraded:
                        degraded.append(caller_pid)
            continue
        parsed = _parse_gis_response({"features": [feature]}, gis_config)
        if (not parsed.get("property_address")
                and not gis_config.get("skip_statewide_fallback")
                and parsed.get("mailing_address")):
            # The county knows where the owner gets mail but not where the property
            # is. Dropping the row here (as the branch below does) threw that mailing
            # address away and let the situs-only statewide layer answer instead, so
            # the lead ended up with a property address and NO mailing — the exact
            # shape of the bug this file is being changed to fix (Codex, 2026-09-10).
            # Keep it and let the statewide layer top up the situs.
            parsed["needs_situs_fallback"] = True
            parsed["parcel_id"] = pid
            for caller_pid in _callers_for(pid, clean_to_originals):
                results[caller_pid] = dict(parsed)
            continue
        if not parsed.get("property_address") and not gis_config.get("skip_statewide_fallback"):
            # No street. For a county that still uses the WA statewide fallback,
            # dropping the row is right — statewide may know the address.
            # But a county with its OWN authoritative layer (King:
            # skip_statewide_fallback) has ALREADY answered: the parcel matched and
            # is vacant/raw land, ~1/3 of King's delinquent parcels. Dropping it
            # there sent it to the statewide service, which returns a WRONG situs
            # (#153, Codex High). Keep it — property_address stays NULL so skip
            # trace is never billed, and the situs city/zip still ride along.
            continue
        parsed["parcel_id"] = pid
        for caller_pid in _callers_for(pid, clean_to_originals):
            results[caller_pid] = dict(parsed)
    return results


# An addressee line is NOT a street. Cowlitz files "ATTN ALAN M ANNIS, DIRECTOR OF
# TAXES" and "C/O LLOYD MILTON PURSLEY" in DEED_HOLDER_ADDRESS_1 while the real street
# sits in _2 (measured 2026-09-10: 53,109 rows street-in-_2-only, 4,473 with _1 as an
# attn/c-o line, 203 with _1 only). Taking _1 blindly would both corrupt the address and
# store a taxpayer NAME, which this codebase deliberately does not collect (the same
# boundary pierce_atip._drop_person_line enforces for ATIP). A real street line starts
# with a house number, a post-office box, or a military/rural route designator.
_MAIL_STREET_SHAPE_RE = re.compile(r"^\s*(?:\d|#|P\.?\s*O\.?\s*B|POB\b|PSC\b|RR\b|HC\b)", re.I)

# An addressee marker glued onto the FRONT of an otherwise good street, which the
# counties do inline as well as in a separate column: "C/O RYAN LLC 10500 NE 8TH ST",
# "ATTN SUSAN CORNELL 981 POWELL AVE SW". Where a street plainly begins further along
# the line, the addressee is dropped and the street kept — losing the whole value would
# throw away a mailable address, and keeping it whole would store a person's name.
_ADDRESSEE_PREFIX_RE = re.compile(
    r"^\s*(?:C\s*/\s*O|ATTN|ATTENTION|DEPT|DEPARTMENT|PROP\s+TAX)\b", re.I
)

# Where the street itself begins, used to cut an addressee prefix at the right point.
# "PO BOX" has to be found as a street start, not skipped past: cutting at the first
# DIGIT instead turned "DEPT OF TRANS PO BOX 330310" into a bare "330310".
_MAIL_STREET_START_RE = re.compile(
    r"(?:\b|(?<=\s))(?:\d|#|P\.?\s*O\.?\s*B|PSC\b|RR\b|HC\b)", re.I
)

_MAIL_PO_BOX_START_RE = re.compile(r"(?:\b|(?<=\s))P\.?\s*O\.?\s*B(?:OX)?\b", re.I)

# Values that are a stand-in for "no data", not an address. UNKNOWN is the token
# address_intel._PLACEHOLDER_STREET_RE already measured in production.
_MAIL_PLACEHOLDER_RE = re.compile(r"^\s*(?:UNKNOWN|NONE|N/?A|NULL)\s*$", re.I)

# A LIKE metacharacter (% or _) in an interpolated value silently widens the
# predicate to unrelated rows. Values carrying one are rejected, not rewritten.
_LIKE_META = re.compile(r"[%_]")


def _attr_text(attrs: dict, field: str | None) -> str | None:
    """One trimmed attribute, or None for null/blank/whitespace."""
    if not field:
        return None
    val = attrs.get(field)
    if val is None:
        return None
    text = str(val).replace("&nbsp;", "").strip()
    return text or None


def _compose_street_parts(attrs: dict, part_fields: list[str]) -> str | None:
    """Join a situs street published as components into one street line.

    Space-joined in the configured order and collapsed, so a null middle component
    ("W" in "5919 W 218TH AVE NE") never leaves a double space.
    """
    parts = [_attr_text(attrs, f) for f in part_fields]
    street = " ".join(p for p in parts if p)
    return " ".join(street.split()) or None


def _compose_mailing(attrs: dict, gis_config: dict) -> str | None:
    """Build "STREET, CITY, ST ZIP" from a street column plus locality columns.

    ``mailing_street_fields`` is tried in order and the FIRST value that looks like a
    street wins (see ``_MAIL_STREET_SHAPE_RE``) — this is a priority list, not a
    concatenation, because the runner-up column is usually an addressee name.

    A locality with no street is dropped rather than stored: "EVERETT, WA 98203" alone
    is not somewhere you can mail a letter, and persisting it would turn an unknown into
    a false positive on the mailing-coverage numbers.
    """
    fields = gis_config.get("mailing_street_fields") or []
    # The shape check exists to CHOOSE between columns. Where a layer designates a
    # single mailing-street column there is nothing to choose, so demanding a
    # house-number start there only discards real addresses: 0.31% of sampled
    # Snohomish taxprline1 values fail it, and some ("ONE ASHLEY WAY") are genuine.
    disambiguating = len(fields) > 1

    street = None
    for field in fields:
        candidate = _attr_text(attrs, field)
        if not candidate or _MAIL_PLACEHOLDER_RE.match(candidate):
            continue
        trimmed = candidate
        if _ADDRESSEE_PREFIX_RE.match(candidate):
            # A post-office box is an unambiguous street start, so it wins over an
            # earlier bare number: in "DEPT 42 PO BOX 330310" the 42 belongs to the
            # addressee, and cutting there stored "42 PO BOX 330310" (Codex P2).
            start = _MAIL_PO_BOX_START_RE.search(candidate) or _MAIL_STREET_START_RE.search(
                candidate)
            trimmed = candidate[start.start():].strip() if start else candidate
        if _MAIL_STREET_SHAPE_RE.match(trimmed):
            street = trimmed
            break
        if not disambiguating and not _ADDRESSEE_PREFIX_RE.match(candidate):
            # Sole designated column, no addressee marker, not a placeholder: a
            # street that simply does not open with a number.
            street = candidate
            break
    if not street:
        return None

    locality = gis_config.get("mailing_locality_fields") or []

    def _loc(i: int) -> str | None:
        return _attr_text(attrs, locality[i]) if i < len(locality) else None

    city, state, zipcode = _loc(0), _loc(1), _loc(2)
    tail = ", ".join(p for p in (city, state) if p)
    if zipcode:
        tail = f"{tail} {zipcode}".strip()
    return f"{street}, {tail}" if tail else street


_CITY_STATE_RE = re.compile(r"^\s*(.+?)\s*,\s*([A-Z]{2})\s*$")
# "PO BOX", "P.O. BOX", "P O BOX", "P.O BOX", "POB" — any post-office box spelling.
_PO_BOX_RE = re.compile(r"^\s*P\.?\s*O\.?\s*B(?:OX)?\b", re.I)


def _situs_parts_from_confirmed_mailing(
    attrs: dict, property_address: str | None, gis_config: dict
) -> dict[str, str | None]:
    """Pierce-style county row (Delivery_Address / City_State / Zipcode = the OWNER's
    mailing): those fields describe the property ONLY when the county itself says
    the mail goes there — Delivery_Address equal to Site_Address after whitespace
    normalization, and not a PO box. Then City_State/Zipcode are evidence-based
    situs parts (Codex-approved derivation); otherwise nothing is emitted."""
    fields = gis_config.get("mailing_fields") or []
    if not property_address or len(fields) < 3:
        return {}
    delivery = " ".join(str(attrs.get(fields[0]) or "").split()).upper()
    site = " ".join(property_address.split()).upper()
    if not delivery or delivery != site or _PO_BOX_RE.match(delivery):
        return {}
    m = _CITY_STATE_RE.match(str(attrs.get(fields[1]) or ""))
    zipcode = str(attrs.get(fields[2]) or "").strip()
    if not m:
        return {}
    return {
        "property_city": m.group(1).strip(),
        "property_state": m.group(2),
        "property_zip": zipcode[:10] or None,
    }


def _situs_parts_direct(
    attrs: dict, gis_config: dict, has_street: bool = False
) -> dict[str, str | None]:
    """Structured situs from a layer that publishes its OWN situs city/state/zip.

    Unlike _situs_parts_from_confirmed_mailing, no inference is needed: these columns
    already describe the property, so nothing has to be proven about where the owner
    receives mail. ``situs_state_literal`` covers a layer with no state column, where
    the state is fixed by which county's service is being queried.
    """
    fields = gis_config.get("situs_part_fields") or []
    if not fields:
        return {}

    def _part(i: int) -> str | None:
        return _attr_text(attrs, fields[i]) if i < len(fields) else None

    city, zipcode = _part(0), _part(2)
    if not city and not zipcode and not _part(1) and not has_street:
        # Nothing located this parcel. `situs_state_literal` alone would assert a
        # state for a row we know nothing else about, and property_state feeds the
        # absentee / out-of-state owner flags — so emit nothing rather than a
        # half-fact the flags would then reason from.
        return {}
    return {
        "property_city": city,
        "property_state": _part(1) or gis_config.get("situs_state_literal") or None,
        "property_zip": zipcode[:10] if zipcode else None,
    }


def _situs_parts(city: str, zipcode: str) -> dict[str, str | None]:
    """Structured SITUS location from a WA statewide row (SITUS_CITY_NM /
    SITUS_ZIP_NR). The state is WA by construction (the service is the WA parcel
    layer, queried with the county FIPS), city/zip only when the row carries them.
    These describe the PROPERTY's location — never the owner's mail."""
    city = " ".join((city or "").split())
    zipcode = (zipcode or "").strip()
    return {
        "property_city": city or None,
        "property_state": "WA",
        "property_zip": zipcode or None,
    }


def _warn_on_arcgis_anomaly(data: object, label: str) -> None:
    """Log the two ArcGIS responses that otherwise read as "no data for these parcels".

    ArcGIS reports a bad query (renamed field, token now required) as HTTP 200 with an
    ``error`` body and no ``features`` key, and a capped page as ``exceededTransferLimit``.
    Both were swallowed as an empty result, so a layer change looked exactly like a
    county that publishes no mailing address. Telemetry only: the caller's handling of
    the features it did get is unchanged.
    """
    if not isinstance(data, dict):
        _logger.warning("%s: response was not a JSON object", label)
        return
    error = data.get("error")
    if error:
        detail = error.get("message") if isinstance(error, dict) else error
        _logger.warning("%s: ArcGIS error in a 200 response: %s", label, str(detail)[:120])
    if data.get("exceededTransferLimit"):
        _logger.warning("%s: ArcGIS truncated the page (exceededTransferLimit)", label)


def _batch_query_county(
    parcel_ids: list[str], gis_config: dict, unreached: list[str] | None = None
) -> dict[str, dict[str, str | None]]:
    """Batch query a county-specific ArcGIS endpoint (has mailing address).

    Results are keyed by the CALLER's parcel id (see _map_county_features). When
    ``unreached`` is given, the caller ids of every chunk that got no usable answer
    are appended to it."""
    endpoint = gis_config["endpoint"]
    parcel_field = gis_config["parcel_field"]
    out_fields = gis_config.get("out_fields", "*")
    results: dict[str, dict] = {}
    chunk_size = 50

    for i in range(0, len(parcel_ids), chunk_size):
        chunk = parcel_ids[i:i + chunk_size]
        # clean (query/server form) -> every caller id that spells it that way.
        clean_to_originals: dict[str, list[str]] = {}
        for pid in chunk:
            if pid and len(pid.strip()) >= 6:
                clean_to_originals.setdefault(pid.replace("-", "").strip(), []).append(pid)
        if not clean_to_originals:
            continue

        in_clause = ",".join(_arcgis_literal(p) for p in clean_to_originals)
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
                _note_unreached(unreached, clean_to_originals)
                continue

            data = resp.json()
            _warn_on_arcgis_anomaly(data, "County GIS batch")
            if not isinstance(data, dict) or data.get("error"):
                # An error body is not an answer about these parcels.
                _note_unreached(unreached, clean_to_originals)
                continue
            degraded: list[str] = []
            found = _map_county_features(
                data.get("features") or [], gis_config, clean_to_originals,
                degraded=degraded,
            )
            results.update(found)
            # One parcel can carry SEVERAL features (condo units), so an empty
            # sibling can sit beside a populated one. A real answer always wins:
            # leaving the id in `degraded` would put it in county_unreached, and
            # mailing_recovery EXCLUDES every unreached parcel from `attempted`, so
            # its actual mailing address would be discarded and the row would rotate
            # forever without ever consuming an attempt (Codex High).
            degraded = [pid for pid in degraded if pid not in found]
            if degraded:
                # The layer answered about these parcels with no data at all. That is
                # not "no mailing address" — it is the source not serving us. Same
                # contract as a timeout: deferable, never a settled negative.
                _logger.warning(
                    "County GIS returned %d attribute-empty feature(s) — source "
                    "degraded, deferring their mailing lookup", len(degraded),
                )
                _note_unreached(unreached, {pid: [pid] for pid in degraded})
            if data.get("exceededTransferLimit"):
                # A capped page is not an answer about the parcels it left out (one
                # parcel can carry several features, e.g. condo units). Those were
                # never looked up, so they must be deferable, not read as "none".
                _note_unreached(unreached, {
                    clean: [pid for pid in originals if pid not in found]
                    for clean, originals in clean_to_originals.items()
                })

            # Count distinct APNs, not fanned-out caller ids, so the ratio is honest.
            # Also report how many carry a STREET: for a county whose vacant/raw-land
            # parcels are deliberately kept (#153), "matched" and "addressable" are
            # different numbers and conflating them hides a layer regression.
            found_apns = {row["parcel_id"] for row in found.values()}
            _with_street = len([v for v in found.values() if v.get("property_address")])
            _logger.info(
                "County GIS batch: %d/%d parcels enriched, %d with a street address",
                len(found_apns), len(clean_to_originals), _with_street
            )

        except Exception as exc:
            _logger.warning("County GIS batch error: %s", str(exc)[:80])
            _note_unreached(unreached, clean_to_originals)

    return results


def _note_unreached(unreached: list[str] | None, clean_to_originals: dict[str, list[str]]) -> None:
    if unreached is None:
        return
    for originals in clean_to_originals.values():
        for pid in originals:
            if pid not in unreached:
                unreached.append(pid)


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

        # Reverse map: query_value -> every caller parcel_id that asked for it. The
        # same _callers_for resolver as the county path, so a returned id that
        # matches no request is dropped rather than filed under itself (Codex P2).
        query_to_originals: dict[str, list[str]] = {}
        for query_value, original in query_pairs:
            callers = query_to_originals.setdefault(query_value, [])
            if original not in callers:
                callers.append(original)
        in_values = list(query_to_originals.keys())

        in_clause = ",".join(_arcgis_literal(p) for p in in_values)
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
            _warn_on_arcgis_anomaly(data, "Statewide GIS batch")
            for feature in data.get("features") or []:
                attrs = feature.get("attributes") or {}
                pid = attrs.get("ORIG_PARCEL_ID")
                address = attrs.get("SITUS_ADDRESS")
                if not pid or not address:
                    continue

                address = " ".join(address.strip().split())
                city = (attrs.get("SITUS_CITY_NM") or "").strip()
                zipcode = (attrs.get("SITUS_ZIP_NR") or "").strip()
                row = {
                    "property_address": address,
                    "mailing_address": None,  # situs-only layer: owner's mail unknown
                    "parcel_id": str(pid),  # canonical format from server
                    **_situs_parts(city, zipcode),
                }
                for caller_pid in _callers_for(pid, query_to_originals):
                    results[caller_pid] = dict(row)

            _logger.info(
                "Statewide GIS batch: %d/%d parcels enriched",
                len({orig for _, orig in query_pairs if orig in results}),
                len({orig for _, orig in query_pairs}),
            )

        except Exception as exc:
            _logger.warning("Statewide GIS batch error: %s", str(exc)[:80])

    return results
