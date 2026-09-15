"""Locate the King County parcel under a code-violation point, strictly.

WHY
---
Seattle SDCI code-violation records (data.seattle.gov ez4a-iug7) carry an address and
a latitude/longitude but no parcel number, so every King code_violation lead reached
the mailing pipeline with parcel_id NULL and could never get a mailing address
(1,060 live leads, 0% mailing on 2026-09-13).

King's public parcel layer answers "which parcel contains this point". A point alone
is not proof: a geocode can sit on a corner lot addressed on the other street, on the
neighbor, or inside a stack of condo polygons. So a parcel is accepted only when
  * exactly one polygon contains the point, and
  * that parcel's own situs street equals the lead's street after USPS normalization
    (house number included, so "2346A" never matches "2346"), and
  * the ZIPs agree when both are known.
Anything else is left unresolved. Measured on a random 40-lead sample: 33 strict
matches (82.5%), and the Assessor extract had a mailing address for 32 of them.

WHAT IT IS NOT
--------------
The resolved PIN is a located FACT stored beside the lead (enrichment_data.kc_pin),
never results.parcel_id: parcel_id is the dedup and billing identity of a delivered
row and must not change underneath it.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from src.scrapers.king_cv_sources import PARCEL_AT_SCRAPE_SOURCES
from src.utils.address_intel import _normalize_street, parse_property_for_display
from src.utils.located_parcel import (
    KING_GIS_POINT_SOURCE,
    MATCH_CONDO_COMPLEX,
    MATCH_EXACT,
    MATCH_STREET_ONLY,
    located_parcel_id,
    mailing_lookup_pin,
)
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get

_logger = setup_logger("scraper.enrichment.king_parcel_locate")

PARCEL_LAYER = (
    "https://gismaps.kingcounty.gov/arcgis/rest/services"
    "/Property/KingCo_PropertyInfo/MapServer/2/query"
)
SOURCE = KING_GIS_POINT_SOURCE
# The owner is the taxpayer named on the King Assessor's eRealProperty page for the PIN.
OWNER_SOURCE = "king_erealproperty"
# King Assessor property type "K" = condominium complex (verified on the layer:
# ZULO CONDOMINIUM, PREUSE_DESC "Condominium(Residential)").
_CONDO_PROPTYPE = "K"
# Worst case for one address-point request (its timeout): the fallback only starts while
# two of them still fit inside the caller's time budget.
_ADDRESS_POINT_WORST_S = 15
# Held back from the fallback for the Assessor extract scan that runs after the lookups.
_EXTRACT_RESERVE_S = 30

# A lead address naming a unit ("#6", "UNIT 6", "APT 6", "STE 6") cannot be proven by a
# street comparison: the normalizer strips units, so two condo units on one base parcel
# would compare equal and one owner's mailing would land on another's lead (Codex P1).
# Scanned across the WHOLE address and tolerant of "Apt. 6" / "Unit-6" (Codex r2 P1). A
# false positive only leaves a lead unmatched, which is the safe direction.
_UNIT_RE = re.compile(
    r"(?:#\s*\w+|\b(?:UNIT|APT|APARTMENT|STE|SUITE|BLDG|BUILDING|SPC|SPACE|LOT|RM|ROOM|FL|FLOOR)"
    r"\b\.?[\s\-]*\w+)", re.I)


@dataclass(frozen=True)
class Located:
    # matched | no_parcel | multiple | address_mismatch | unit_address | no_coordinates | error
    status: str
    pin: str | None = None
    parcel_address: str | None = None
    # For a match: "exact" when both ZIPs were known and equal, "street_only" when one
    # side had no ZIP (the street still matched), "condo_complex" when the parcel is a
    # whole condominium. "exact" and "street_only" are shown and name the owner
    # (src/utils/located_parcel.py); paid skip trace accepts "exact" only.
    match: str | None = None
    # (PIN, PROPTYPE) of every polygon the layer returned under the point, when that set
    # is complete; the address-point fallback reuses it instead of asking again.
    point_parcels: tuple[tuple[str, str], ...] | None = None


def _street_and_zip(address: str | None) -> tuple[str, str]:
    parsed = parse_property_for_display(address or "")
    return _normalize_street(parsed.get("street") or (address or "").split(",")[0]), (
        (parsed.get("zip") or "")[:5])


def locate(lat: object, lon: object, property_address: str | None) -> Located:
    """The single parcel under (lat, lon) whose situs is this lead's address, if any."""
    # Missing or unusable coordinates are a property of the stored record, not a passing
    # failure: retrying can never locate them, so they get a terminal status instead of
    # "error" (15 prod rows with JSON-null coordinates were revisited on every run).
    try:
        lat_f, lon_f = float(str(lat).strip()), float(str(lon).strip())
    except (TypeError, ValueError):
        return Located("no_coordinates")
    if not (math.isfinite(lat_f) and math.isfinite(lon_f)
            and -90 <= lat_f <= 90 and -180 <= lon_f <= 180):
        return Located("no_coordinates")
    if _UNIT_RE.search(property_address or ""):
        return Located("unit_address")
    try:
        resp = safe_get(PARCEL_LAYER, params={
            "geometry": f"{lon_f},{lat_f}", "geometryType": "esriGeometryPoint",
            "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
            "outFields": "PIN,ADDR_FULL,ZIP5,PROPTYPE", "returnGeometry": "false", "f": "json",
        }, headers={"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"}, timeout=15)
        data = resp.json() if resp.status_code == 200 else None
    except Exception as exc:  # noqa: BLE001 -- one failed lookup must not stop a batch
        if type(exc).__name__ in ("SoftTimeLimitExceeded", "TimeLimitExceeded"):
            raise
        _logger.warning("King parcel locate failed: %s", str(exc)[:120])
        return Located("error")
    if not isinstance(data, dict) or data.get("error"):
        return Located("error")
    if data.get("exceededTransferLimit"):
        # A capped page does not prove "exactly one polygon" (Codex P1).
        return Located("multiple")
    features = data.get("features") or []
    point_parcels = tuple(
        (str((f.get("attributes") or {}).get("PIN") or "").strip(),
         str((f.get("attributes") or {}).get("PROPTYPE") or "").strip().upper())
        for f in features)
    if not features:
        return Located("no_parcel", point_parcels=point_parcels)
    if len(features) != 1:
        return Located("multiple", point_parcels=point_parcels)
    attrs = features[0].get("attributes") or {}
    pin = str(attrs.get("PIN") or "").strip()
    parcel_street = _normalize_street(attrs.get("ADDR_FULL"))
    lead_street, lead_zip = _street_and_zip(property_address)
    parcel_zip = str(attrs.get("ZIP5") or "").strip()[:5]
    if (not pin or not pin.isdigit() or len(pin) != 10 or not parcel_street
            or parcel_street != lead_street
            or (lead_zip and parcel_zip and lead_zip != parcel_zip)):
        return Located("address_mismatch", parcel_address=attrs.get("ADDR_FULL"),
                       point_parcels=point_parcels)
    if str(attrs.get("PROPTYPE") or "").strip().upper() == _CONDO_PROPTYPE:
        # The whole-complex parcel (minor 0000): every unit sits under this polygon and
        # shares its street, so neither the PIN nor its taxpayer identifies the unit
        # owner the complaint is about.
        match = MATCH_CONDO_COMPLEX
    elif lead_zip and parcel_zip:
        match = MATCH_EXACT
    else:
        match = MATCH_STREET_ONLY
    return Located("matched", pin=pin, parcel_address=attrs.get("ADDR_FULL"), match=match)


def locate_many(items: list[tuple[str, object, object, str | None]], *,
                pace_s: float = 0.25, budget_s: float | None = None) -> dict[str, Located]:
    """Locate (key, lat, lon, address) items sequentially, paced, within a time budget.

    Keys not reached before the budget runs out are simply absent from the result.
    """
    deadline = time.monotonic() + budget_s if budget_s is not None else None
    out: dict[str, Located] = {}
    for key, lat, lon, address in items:
        if deadline is not None and time.monotonic() >= deadline:
            break
        out[key] = locate(lat, lon, address)
        time.sleep(pace_s)
    return out


def owner_parcel_id(res, *, exact_only: bool = False) -> str | None:
    """The King PIN whose Assessor taxpayer may name this code-violation row's owner.

    A source that prints the PIN (Bellevue, Burien, King County Accela) stored it as
    parcel_id at scrape, so that PIN is the parcel, provided it is the 10-digit form the
    scraper wrote. Every other row (Seattle SDCI) has only a located PIN, and only a shown
    location counts (exact, street-level or address point, see src/utils/located_parcel.py);
    ``exact_only`` narrows that to what the located-parcel rule allows to spend money.
    """
    ed = getattr(res, "enrichment_data", None)
    if isinstance(ed, dict) and ed.get("source") in PARCEL_AT_SCRAPE_SOURCES:
        pin = getattr(res, "parcel_id", None)
        return pin if isinstance(pin, str) and len(pin) == 10 and pin.isdigit() else None
    return located_parcel_id(ed, exact_only=exact_only)


def owner_lookup_pins(rows) -> dict[str, list]:
    """{owner PIN: [rows]} for code-violation rows that still have no owner.

    Only a PIN the source printed, or a shown location (exact, street-level or address point, see
    src/utils/located_parcel.py), may name the owner: the county's taxpayer on a parcel
    we are not sure of would put a stranger's name on the lead. A row that already has a
    party_name is never offered for replacement.

    Printed-PIN parcels come first in the returned order, which is the order the
    time-budgeted owner pass asks King in: a printed parcel is the one we are sure of.
    A row of either kind this pass does not reach is named later by the
    cv_owner_recovery sweep.
    """
    printed: dict[str, list] = {}
    located: dict[str, list] = {}
    for res in rows:
        if res.party_name:
            continue
        pin = owner_parcel_id(res)
        if not pin:
            continue
        ed = getattr(res, "enrichment_data", None)
        is_printed = isinstance(ed, dict) and ed.get("source") in PARCEL_AT_SCRAPE_SOURCES
        (printed if is_printed else located).setdefault(pin, []).append(res)
    out = dict(printed)
    for pin, pin_rows in located.items():
        out.setdefault(pin, []).extend(pin_rows)
    return out


def apply_owner_names(pin_map: dict[str, list], owners: dict[str, str], *,
                      checked_at: str) -> int:
    """Write each resolved owner onto its rows; returns how many rows were named.

    `owners` is batch_extract_king_owners' answer ({pin: name}), which only carries
    names read from a page the county served for that same PIN. Re-checks both guards
    on the row itself so a row changed since selection is left alone.
    """
    named = 0
    for pin, owner in owners.items():
        name = (owner or "").strip()[:512]
        if not name:
            continue
        for res in pin_map.get(pin, []):
            if res.party_name or owner_parcel_id(res) != pin:
                continue
            res.party_name = name
            ed = dict(res.enrichment_data)
            ed.update({"owner_source": OWNER_SOURCE, "owner_pin": pin,
                       "owner_checked_at": checked_at})
            res.enrichment_data = ed
            named += 1
    return named


def resolve_code_violation_mailing(
    items: list[tuple[str, object, object, str | None]], *,
    pace_s: float = 0.25, budget_s: float | None = None, address_points: bool = False,
    property_zips: dict[str, str | None] | None = None,
) -> tuple[dict[str, dict], str | None]:
    """For each (key, lat, lon, address): locate the parcel, then its extract mailing.

    Returns ({key: decision}, extract snapshot). A decision always carries `kc_pin_status`
    for a definite outcome (so a row is never re-located forever; a transient "error" is
    left without one so it retries) and, when matched, `kc_pin`/`kc_parcel_address`/
    `kc_pin_match`/`kc_pin_source`. With ``address_points``, a point outcome the address
    points may resolve (`king_address_points.FALLBACK_STATUSES`) is retried there within
    the same budget, and a definite answer adds `kc_address_point_evidence`.
    `mailing_address` is present only for a tier `located_parcel.mailing_lookup_pin`
    allows, with an unambiguous extract answer. Keys the budget did not reach are absent.
    """
    from src.scrapers.enrichment import king_address_points as kap
    from src.scrapers.enrichment.king_rpacct import resolve_pins

    deadline = time.monotonic() + budget_s if budget_s is not None else None
    zips = property_zips or {}
    # One lookup per distinct point+address(+ZIP column): a complaint often has several records.
    by_point: dict[tuple, list[tuple[str, str | None]]] = {}
    for key, lat, lon, address in items:
        by_point.setdefault((str(lat), str(lon), (address or "").strip().upper(),
                             str(zips.get(key) or "").strip()), []).append((key, address))
    located = locate_many([(group[0][0], pt[0], pt[1], group[0][1])
                           for pt, group in by_point.items()], pace_s=pace_s, budget_s=budget_s)
    checked_at = datetime.now(UTC).isoformat()
    decisions: dict[str, dict] = {}
    for (lat, lon, _, prop_zip), group in by_point.items():
        first_key, first_address = group[0]
        loc = located.get(first_key)
        # Not reached, or a transient failure: no status, so a later run retries it.
        if loc is None or loc.status == "error":
            continue
        d = {"kc_pin_status": loc.status}
        if loc.status == "matched":
            d.update({"kc_pin": loc.pin, "kc_parcel_address": loc.parcel_address,
                      "kc_pin_match": loc.match, "kc_pin_source": SOURCE})
        elif (address_points and loc.status in kap.FALLBACK_STATUSES
              # Room for both address-point requests to time out, and for the extract
              # scan that follows, before the deadline.
              and (deadline is None
                   or time.monotonic() + 2 * (_ADDRESS_POINT_WORST_S + pace_s)
                   + _EXTRACT_RESERVE_S < deadline)):
            ap = kap.match_address_point(lat, lon, first_address,
                                         point_parcels=loc.point_parcels,
                                         point_status=loc.status, pace_s=pace_s,
                                         property_zip=prop_zip or None)
            d.update(kap.decision_fields(ap, checked_at=checked_at))
            time.sleep(pace_s)
        for key, _address in group:
            decisions[key] = dict(d)
    pins = {pin for d in decisions.values() if (pin := mailing_lookup_pin(d))}
    snapshot = None
    if pins:
        resolved = resolve_pins(pins)
        if resolved is None:
            # The extract is temporarily unusable. A matched row stamped now would be
            # excluded from every later run with no mailing (Codex P1), so a strict match
            # carries no status and retries; an address-point match falls back to its
            # point outcome with no evidence, so the address-point repair retries it.
            kept: dict[str, dict] = {}
            for k, d in decisions.items():
                if d["kc_pin_status"] != "matched":
                    kept[k] = d
                elif d.get("kc_pin_source") == kap.SOURCE:
                    kept[k] = {"kc_pin_status": d[kap.EVIDENCE_KEY]["point_status"]}
            decisions = kept
        else:
            answers, snapshot = resolved
            for d in decisions.values():
                ans = answers.get(mailing_lookup_pin(d))
                if ans is not None and ans.status == "found":
                    d["mailing_address"] = ans.mailing_address
    return decisions, snapshot
