"""King County address points: the County's own address-to-PIN answer, applied strictly.

WHY
---
The strict point rule in `king_parcel_locate` compares a code-violation address with the
ONE situs address King's parcel layer carries per polygon. A corner lot, a building with
several street numbers, or a townhome written "9043 A" instead of "9043A" fails that
comparison even when the complaint is plainly about that parcel (365 prod rows were
address_mismatch on 2026-09-14). King's address-point layer lists every address the
County has assigned, each with its PIN, so it can prove the parcel instead.

THE RULE (owner approved, Codex consult gate passed)
----------------------------------------------------
  * The lead address is normalized (upper case, USPS suffixes and directionals, "M L
    KING JR" spelled out). A single letter after the house number is read BOTH ways:
    a unit letter ("2605 E 22ND AVE W" -> 2605E) and, for N/S/E/W, a predirectional
    ("2571 W MONTLAKE PL E"). Units, number ranges and fractions are never matched.
  * A point matches only on the exact house token (number + letter) and the exact
    compressed street name, in Seattle. No fuzzy or substring matching.
  * Every matching point must name the SAME valid PIN; any null, malformed or second
    PIN rejects. When the lead has a ZIP, every matching point's ZIP must equal it.
  * A lead with no letter where King only has lettered siblings (1212A, 1212B) is
    ambiguous and rejects.
  * With coordinates, the PIN must be one of the parcel polygons containing the point
    (this rejects land-only parcels and neighboring buildings on multi-parcel sites):
    tier `address_point`, shown and named. Without coordinates: tier `address_only`,
    an internal candidate that is not shown, not named and gets no mailing.
  * A condominium complex parcel (PROPTYPE K, or the point is the County's condo
    complex extract) is `condo_complex`: never shown, never named.

A definite outcome is recorded in `kc_address_point_evidence` so the row is not asked
again; a transient failure records nothing so a later run retries it.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field

from src.api.middleware.security import add_scrape_domain
from src.utils.address_intel import _normalize_street
from src.utils.located_parcel import (
    KING_GIS_ADDRESS_POINT_SOURCE,
    MATCH_ADDRESS_ONLY,
    MATCH_ADDRESS_POINT,
    MATCH_CONDO_COMPLEX,
)
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get

_logger = setup_logger("scraper.enrichment.king_address_points")

GIS_HOST = "gismaps.kingcounty.gov"
add_scrape_domain(GIS_HOST)
ADDRESS_POINT_LAYER = (
    f"https://{GIS_HOST}/arcgis/rest/services/Address/KingCo_AddressPoints/MapServer/0/query"
)
PARCEL_LAYER = f"https://{GIS_HOST}/arcgis/rest/services/Property/KingCo_PropertyInfo/MapServer/2/query"
SOURCE = KING_GIS_ADDRESS_POINT_SOURCE
EVIDENCE_KEY = "kc_address_point_evidence"
# Point-rule outcomes the address points may resolve. unit_address is deliberately absent.
FALLBACK_STATUSES = frozenset({"address_mismatch", "multiple", "no_parcel", "no_coordinates"})
_CITY = "Seattle"  # SDCI is the City of Seattle; the same street exists in other cities
_CONDO_PROPTYPE = "K"
_CONDO_FILTER = "CONDOCOMPLEX_EXTR"
_DIRECTION_LETTERS = frozenset("NSEW")
_HEADERS = {"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"}
_TIMEOUT_S = 15

# Same unit detector as the strict point rule: a unit cannot be proven by a street match.
_UNIT_RE = re.compile(
    r"(?:#\s*\w+|\b(?:UNIT|APT|APARTMENT|STE|SUITE|BLDG|BUILDING|SPC|SPACE|LOT|RM|ROOM|FL|FLOOR)"
    r"\b\.?[\s\-]*\w+)", re.I)
_RANGE_RE = re.compile(r"^\s*\d+\s*(?:-|/|\s\d+/)")
_MLK_RE = re.compile(r"\b(?:M\s*L\s*K(?:ING)?|MARTIN\s+L(?:UTHER)?\s+KING)(?:\s+JR)?\b")
_ZIP_TAIL_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\s*$")
_ZIP_RE = re.compile(r"\d{5}(?:-?\d{4})?")
_STATE_TAIL_RE =re.compile(r"\s+(?:SEATTLE\s+)?WA(?:\s+(\d{5})(?:-\d{4})?)?\s*$")
_HOUSE_RE =re.compile(r"^(\d{1,6})(?:\s?([A-Z]))?\s+(\S.*)$")
_COMPRESS_OK = re.compile(r"^[A-Z0-9]{1,80}$")


@dataclass(frozen=True)
class ParsedAddress:
    normalized: str
    number: int
    letter: str | None
    street: str
    zip5: str | None


@dataclass(frozen=True)
class Reading:
    name: str  # "unit_letter" | "directional" | "none"
    house: str  # first token of King's ADDR_FULL, e.g. "9043A" or "2571"
    compress: str  # King's COMPRESS_NAME, e.g. "WMONTLAKEPLE"


@dataclass(frozen=True)
class AddressPointDecision:
    outcome: str  # "accepted" | "condo_complex" | "rejected" | "error"
    pin: str | None = None
    match: str | None = None
    parcel_address: str | None = None
    evidence: dict = field(default_factory=dict)


def parse_lead_address(address: str | None) -> ParsedAddress | None:
    """The lead's house number, optional letter, street and ZIP; None when not matchable."""
    raw = (address or "").strip().upper()
    if not raw or _UNIT_RE.search(raw) or _RANGE_RE.match(raw):
        return None
    head, _, tail = raw.partition(",")
    zip_m = _ZIP_TAIL_RE.search(tail) if tail else None
    # SDCI sometimes appends the city/state without a comma ("... WAY S WA").
    state_m = _STATE_TAIL_RE.search(head)
    if state_m:
        head = head[:state_m.start()]
        zip_m = zip_m or (state_m if state_m.group(1) else None)
    street_part = _MLK_RE.sub("MARTIN LUTHER KING JR", head.replace(".", " "))
    normalized = _normalize_street(street_part)
    m = _HOUSE_RE.match(normalized)
    if not m:
        return None
    number, letter, street = m.group(1), m.group(2), m.group(3).strip()
    if not street or not _COMPRESS_OK.match(street.replace(" ", "")):
        return None
    return ParsedAddress(normalized=normalized, number=int(number), letter=letter,
                         street=street, zip5=zip_m.group(1) if zip_m else None)


def readings(parsed: ParsedAddress) -> list[Reading]:
    """Every way King could have written this address."""
    num = str(parsed.number)
    compress = parsed.street.replace(" ", "")
    if parsed.letter is None:
        return [Reading("none", num, compress)]
    out = [Reading("unit_letter", num + parsed.letter, compress)]
    if parsed.letter in _DIRECTION_LETTERS:
        out.append(Reading("directional", num, parsed.letter + compress))
    return out


def _valid_pin(value: object) -> str | None:
    pin = str(value or "").strip()
    return pin if len(pin) == 10 and pin.isdigit() else None


def _house_token(attrs: dict) -> str:
    return str(attrs.get("ADDR_FULL") or "").strip().upper().split(" ")[0]


def _query(url: str, params: dict) -> dict | None:
    """The layer's JSON answer, or None for any transient failure."""
    try:
        resp = safe_get(url, params={**params, "returnGeometry": "false", "f": "json"},
                        headers=_HEADERS, timeout=_TIMEOUT_S, require_allowlisted=True)
        data = resp.json() if resp.status_code == 200 else None
    except Exception as exc:  # noqa: BLE001 -- one failed lookup must not stop a batch
        if type(exc).__name__ in ("SoftTimeLimitExceeded", "TimeLimitExceeded"):
            raise
        _logger.warning("King address point lookup failed: %s", str(exc)[:120])
        return None
    if not isinstance(data, dict) or data.get("error"):
        return None
    features = data.get("features")
    # A body without a well-formed feature list is a broken answer, not "no match"
    # (Codex r2 P2): treating it as empty would stamp a terminal rejection. ArcGIS returns
    # every requested field on every feature (null when empty), so a missing one is too.
    required = [f for f in params.get("outFields", "").split(",") if f]
    if not isinstance(features, list) or not all(
            isinstance(f, dict) and isinstance(f.get("attributes"), dict)
            and all(k in f["attributes"] for k in required) for f in features):
        return None
    return data


def parcels_under_point(lat: float, lon: float) -> tuple[tuple[str, str], ...] | None:
    """(PIN, PROPTYPE) of every parcel polygon containing the point; None when unknown."""
    data = _query(PARCEL_LAYER, {
        "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects", "outFields": "PIN,PROPTYPE"})
    if data is None or data.get("exceededTransferLimit"):
        return None
    return tuple((str((f.get("attributes") or {}).get("PIN") or "").strip(),
                  str((f.get("attributes") or {}).get("PROPTYPE") or "").strip().upper())
                 for f in data.get("features") or [])


def _coordinates(lat: object, lon: object) -> tuple[float, float] | None:
    try:
        lat_f, lon_f = float(str(lat).strip()), float(str(lon).strip())
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat_f) and math.isfinite(lon_f)
            and -90 <= lat_f <= 90 and -180 <= lon_f <= 180):
        return None
    return lat_f, lon_f


def match_address_point(
    lat: object, lon: object, address: str | None, *,
    point_parcels: tuple[tuple[str, str], ...] | None = None,
    point_status: str | None = None, pace_s: float = 0.25, property_zip: str | None = None,
) -> AddressPointDecision:
    """Resolve one lead through King's address points under the rule in the module doc.

    ``point_parcels`` are the (PIN, PROPTYPE) polygons the strict point rule already saw
    under these coordinates, to avoid asking twice; None means ask. ``property_zip`` is
    the lead's ZIP column: every ZIP the lead carries must agree and must equal King's.
    """
    ev: dict = {"point_status": point_status}

    def rejected(reason: str) -> AddressPointDecision:
        return AddressPointDecision("rejected", evidence={**ev, "outcome": "rejected",
                                                          "reason": reason})

    parsed = parse_lead_address(address)
    if parsed is None:
        return rejected("unit_address" if _UNIT_RE.search(address or "") else "unparseable_address")
    raw_zip = str(property_zip or "").strip()
    column_zip = raw_zip[:5]
    ev.update({"normalized_address": parsed.normalized, "lead_zip": parsed.zip5,
               "property_zip": raw_zip or None})
    if raw_zip and not _ZIP_RE.fullmatch(raw_zip):
        return rejected("invalid_property_zip")
    lead_zips = {z for z in (parsed.zip5, column_zip) if z}
    if len(lead_zips) > 1:
        ev["zip_compare"] = "lead_zips_disagree"
        return rejected("zip_conflict")
    lead_zip = next(iter(lead_zips), None)
    reads = readings(parsed)
    compresses = sorted({r.compress for r in reads})
    data = _query(ADDRESS_POINT_LAYER, {
        "where": (f"ADDR_NUM={parsed.number} AND CTYNAME='{_CITY}' AND COMPRESS_NAME IN ("
                  + ",".join(f"'{c}'" for c in compresses) + ")"),
        "outFields": "PIN,ADDR_FULL,ADDR_NUM,COMPRESS_NAME,ZIP5,PRIM_ADDR_FILTER,Unit,Building,CTYNAME",
    })
    if data is None:
        return AddressPointDecision("error")
    if data.get("exceededTransferLimit"):
        # A capped page does not prove "exactly one PIN": not a definite answer, retry.
        return AddressPointDecision("error")
    points = [f.get("attributes") or {} for f in data.get("features") or []]
    hits: list[tuple[Reading, dict]] = []
    lettered_only = False
    for rd in reads:
        same_street = [p for p in points if p.get("ADDR_NUM") == parsed.number
                       and str(p.get("COMPRESS_NAME") or "").strip().upper() == rd.compress
                       and str(p.get("CTYNAME") or "").strip().lower() == _CITY.lower()]
        exact = [p for p in same_street if _house_token(p) == rd.house]
        hits.extend((rd, p) for p in exact)
        if (not exact and rd.house == str(parsed.number)
                and any(re.fullmatch(rf"{parsed.number}[A-Z]", _house_token(p)) for p in same_street)):
            lettered_only = True
    used = sorted({rd.name for rd, _ in hits})
    ev["letter_reading"] = used[0] if len(used) == 1 else ("both" if used else None)
    ev["address_point_pins"] = sorted({str(p.get("PIN") or "") for _, p in hits})
    if not hits:
        return rejected("lettered_only" if lettered_only else "no_address_point")
    if any(str(p.get("Unit") or "").strip() or str(p.get("Building") or "").strip()
           for _, p in hits):
        return rejected("unit_point")
    pins = {_valid_pin(p.get("PIN")) for _, p in hits}
    if None in pins:
        return rejected("no_pin" if pins == {None} else "ambiguous_pin")
    if len(pins) != 1:
        return rejected("multiple_pins")
    pin = next(iter(pins))
    if lead_zip is None:
        ev["zip_compare"] = "lead_has_no_zip"
    elif all(str(p.get("ZIP5") or "").strip()[:5] == lead_zip for _, p in hits):
        ev["zip_compare"] = "equal"
    else:
        ev["zip_compare"] = "conflict"
        return rejected("zip_conflict")

    coords = _coordinates(lat, lon)
    if coords is not None:
        if point_parcels is None:
            time.sleep(pace_s)
            point_parcels = parcels_under_point(*coords)
            if point_parcels is None:
                return AddressPointDecision("error")
        ev["coordinate_pins"] = sorted(p for p, _ in point_parcels)
        pin_proptypes = {t for p, t in point_parcels if p == pin}
        if not pin_proptypes:
            return rejected("not_under_coordinates")
        tier = MATCH_ADDRESS_POINT
        proptype = _CONDO_PROPTYPE if _CONDO_PROPTYPE in pin_proptypes else pin_proptypes.pop()
    else:
        ev["coordinate_pins"] = None
        time.sleep(pace_s)
        data = _query(PARCEL_LAYER, {"where": f"PIN='{pin}'", "outFields": "PIN,PROPTYPE"})
        if data is None or data.get("exceededTransferLimit"):
            return AddressPointDecision("error")
        found = [f.get("attributes") or {} for f in data.get("features") or []
                 if _valid_pin((f.get("attributes") or {}).get("PIN")) == pin]
        if not found:
            return rejected("pin_not_in_parcel_layer")
        tier = MATCH_ADDRESS_ONLY
        proptypes_found = {str(a.get("PROPTYPE") or "").strip().upper() for a in found}
        # Any condo polygon for this PIN makes it a condo complex.
        proptype = _CONDO_PROPTYPE if _CONDO_PROPTYPE in proptypes_found else proptypes_found.pop()
    parcel_address = str(hits[0][1].get("ADDR_FULL") or "").strip() or None
    if proptype == _CONDO_PROPTYPE or any(
            str(p.get("PRIM_ADDR_FILTER") or "").strip().upper() == _CONDO_FILTER for _, p in hits):
        # The complex parcel names no unit owner; the complaint's unit is unknown.
        return AddressPointDecision("condo_complex", pin=pin, match=MATCH_CONDO_COMPLEX,
                                    parcel_address=parcel_address,
                                    evidence={**ev, "outcome": "condo_complex", "reason": None,
                                              "tier": MATCH_CONDO_COMPLEX})
    return AddressPointDecision("accepted", pin=pin, match=tier, parcel_address=parcel_address,
                                evidence={**ev, "outcome": "accepted", "reason": None, "tier": tier})


def decision_fields(decision: AddressPointDecision, *, checked_at: str) -> dict:
    """The enrichment_data keys to write for a decision; {} for a transient error."""
    if decision.outcome == "error":
        return {}
    out: dict = {EVIDENCE_KEY: {**decision.evidence, "checked_at": checked_at}}
    if decision.pin:
        out.update({"kc_pin_status": "matched", "kc_pin": decision.pin,
                    "kc_parcel_address": decision.parcel_address,
                    "kc_pin_match": decision.match, "kc_pin_source": SOURCE})
    return out
