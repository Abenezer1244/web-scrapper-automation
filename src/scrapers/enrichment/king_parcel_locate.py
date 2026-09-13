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

import re
import time
from dataclasses import dataclass

from src.utils.address_intel import _normalize_street, parse_property_for_display
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get

_logger = setup_logger("scraper.enrichment.king_parcel_locate")

PARCEL_LAYER = (
    "https://gismaps.kingcounty.gov/arcgis/rest/services"
    "/Property/KingCo_PropertyInfo/MapServer/2/query"
)
SOURCE = "king_gis_point_in_parcel"

# A lead address naming a unit ("#6", "UNIT 6", "APT 6", "STE 6") cannot be proven by a
# street comparison: the normalizer strips units, so two condo units on one base parcel
# would compare equal and one owner's mailing would land on another's lead (Codex P1).
_UNIT_RE = re.compile(r"(?:#\s*\w+|\b(?:UNIT|APT|APARTMENT|STE|SUITE|BLDG|SPC|LOT)\s+\w+)", re.I)


@dataclass(frozen=True)
class Located:
    status: str      # matched | no_parcel | multiple | address_mismatch | unit_address | error
    pin: str | None = None
    parcel_address: str | None = None


def _street_and_zip(address: str | None) -> tuple[str, str]:
    parsed = parse_property_for_display(address or "")
    return _normalize_street(parsed.get("street") or (address or "").split(",")[0]), (
        (parsed.get("zip") or "")[:5])


def locate(lat: object, lon: object, property_address: str | None) -> Located:
    """The single parcel under (lat, lon) whose situs is this lead's address, if any."""
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return Located("error")
    if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
        return Located("error")
    if _UNIT_RE.search((property_address or "").split(",")[0]):
        return Located("unit_address")
    try:
        resp = safe_get(PARCEL_LAYER, params={
            "geometry": f"{lon_f},{lat_f}", "geometryType": "esriGeometryPoint",
            "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
            "outFields": "PIN,ADDR_FULL,ZIP5", "returnGeometry": "false", "f": "json",
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
    if not features:
        return Located("no_parcel")
    if len(features) != 1:
        return Located("multiple")
    attrs = features[0].get("attributes") or {}
    pin = str(attrs.get("PIN") or "").strip()
    parcel_street = _normalize_street(attrs.get("ADDR_FULL"))
    lead_street, lead_zip = _street_and_zip(property_address)
    parcel_zip = str(attrs.get("ZIP5") or "").strip()[:5]
    if (not pin or not pin.isdigit() or len(pin) != 10 or not parcel_street
            or parcel_street != lead_street
            or (lead_zip and parcel_zip and lead_zip != parcel_zip)):
        return Located("address_mismatch", parcel_address=attrs.get("ADDR_FULL"))
    return Located("matched", pin=pin, parcel_address=attrs.get("ADDR_FULL"))


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


def resolve_code_violation_mailing(
    items: list[tuple[str, object, object, str | None]], *,
    pace_s: float = 0.25, budget_s: float | None = None,
) -> tuple[dict[str, dict], str | None]:
    """For each (key, lat, lon, address): locate the parcel, then its extract mailing.

    Returns ({key: decision}, extract snapshot). A decision always carries `kc_pin_status`
    for a definite outcome (so a row is never re-located forever; a transient "error" is
    left without one so it retries) and, when matched, `kc_pin`/`kc_parcel_address`;
    `mailing_address` is present only for a strict parcel match with an unambiguous
    extract answer. Keys the budget did not reach are absent.
    """
    from src.scrapers.enrichment.king_rpacct import resolve_pins

    # One lookup per distinct point+address: a complaint often has several records.
    by_point: dict[tuple, list[str]] = {}
    for key, lat, lon, address in items:
        by_point.setdefault((str(lat), str(lon), (address or "").strip().upper()), []).append(key)
    located = locate_many([(k, pt[0], pt[1], pt[2]) for pt, keys in by_point.items()
                           for k in keys[:1]], pace_s=pace_s, budget_s=budget_s)
    decisions: dict[str, dict] = {}
    for keys in by_point.values():
        loc = located.get(keys[0])
        # Not reached, or a transient failure: no status, so a later run retries it.
        if loc is None or loc.status == "error":
            continue
        for key in keys:
            d = {"kc_pin_status": loc.status}
            if loc.status == "matched":
                d.update({"kc_pin": loc.pin, "kc_parcel_address": loc.parcel_address})
            decisions[key] = d
    pins = {d["kc_pin"] for d in decisions.values() if d.get("kc_pin")}
    snapshot = None
    if pins:
        resolved = resolve_pins(pins)
        if resolved is None:
            # The extract is temporarily unusable. A matched row stamped now would be
            # excluded from every later run with no mailing (Codex P1), so matched rows
            # carry no status and retry; definite non-matches keep theirs.
            decisions = {k: d for k, d in decisions.items() if d["kc_pin_status"] != "matched"}
        else:
            answers, snapshot = resolved
            for d in decisions.values():
                ans = answers.get(d.get("kc_pin"))
                if ans is not None and ans.status == "found":
                    d["mailing_address"] = ans.mailing_address
    return decisions, snapshot
