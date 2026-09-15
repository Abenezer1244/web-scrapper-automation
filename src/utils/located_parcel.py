"""The parcel a lead was LOCATED on, when that location is strict enough to show.

Seattle SDCI code violations carry no parcel number. `king_parcel_locate` finds the
King County parcel under the complaint's coordinates and stores it beside the lead as
`enrichment_data.kc_pin`, never in `results.parcel_id`: parcel_id feeds the frozen
billing dedup_hash, and both same-run sibling collapse and enrichment reuse recompute
that hash from the CURRENT parcel_id, so writing a PIN there would bill a property twice.

This module is the single read-side rule for showing that PIN as a Parcel ID (API and
export), naming its owner, and filling its mailing address. Callers never read
`kc_pin_match` themselves. Tiers (owner decisions 2026-09-14):
  * exact: one parcel polygon under the point, the same normalized street including
    house number, and the lead's ZIP equal to the parcel's ZIP;
  * street_only: the same, but the source row carried no ZIP to compare (5/5 verified);
  * address_point: King's own address point for this exact address names exactly one
    PIN, and that PIN is one of the polygons under the complaint's coordinates
    (`king_address_points`). Shown and named, never paid for;
  * address_only: the same address-point answer, but the row has no coordinates to
    corroborate it. An internal candidate only: not shown, not named, no mailing;
  * condo_complex: a whole condominium complex parcel. Never shown or named.
Paid skip trace accepts EXACT only (Codex P1): anything weaker is not worth a charge.
"""
from __future__ import annotations

from typing import Any

KING_GIS_POINT_SOURCE = "king_gis_point_in_parcel"
KING_GIS_ADDRESS_POINT_SOURCE = "king_gis_address_point"
MATCH_EXACT = "exact"
MATCH_STREET_ONLY = "street_only"
MATCH_ADDRESS_POINT = "address_point"
MATCH_ADDRESS_ONLY = "address_only"
MATCH_CONDO_COMPLEX = "condo_complex"
PARCEL_SOURCE_LABEL = "County parcel map match"
PARCEL_SOURCE_LABEL_STREET_ONLY = "County parcel map match (street only, no ZIP in source)"
PARCEL_SOURCE_LABEL_ADDRESS_POINT = "County address match"
# Which tiers each locating source may produce. A tier stamped by the other source is
# not trusted: the strict point rule and the address-point rule prove different things.
_SHOWN_MATCHES_BY_SOURCE = {
    KING_GIS_POINT_SOURCE: frozenset({MATCH_EXACT, MATCH_STREET_ONLY}),
    KING_GIS_ADDRESS_POINT_SOURCE: frozenset({MATCH_ADDRESS_POINT}),
}
# Tiers whose PIN may be used to look up a mailing address. Condo complexes located by
# the strict point rule keep the pre-existing behavior; nothing internal-only qualifies.
_MAILING_MATCHES_BY_SOURCE = {
    KING_GIS_POINT_SOURCE: frozenset({MATCH_EXACT, MATCH_STREET_ONLY, MATCH_CONDO_COMPLEX}),
    KING_GIS_ADDRESS_POINT_SOURCE: frozenset({MATCH_ADDRESS_POINT}),
}
_LABELS = {
    MATCH_EXACT: PARCEL_SOURCE_LABEL,
    MATCH_STREET_ONLY: PARCEL_SOURCE_LABEL_STREET_ONLY,
    MATCH_ADDRESS_POINT: PARCEL_SOURCE_LABEL_ADDRESS_POINT,
}


def _matched_tier(enrichment_data: Any, allowed: dict[str, frozenset]) -> str | None:
    if not isinstance(enrichment_data, dict):
        return None
    pin = enrichment_data.get("kc_pin")
    if not (isinstance(pin, str) and len(pin) == 10 and pin.isdigit()):
        return None
    match = enrichment_data.get("kc_pin_match")
    source = enrichment_data.get("kc_pin_source")
    if (enrichment_data.get("kc_pin_status") != "matched"
            # isinstance first: a malformed list/object value is unhashable (Codex P2).
            or not isinstance(source, str) or not isinstance(match, str)
            or match not in allowed.get(source, frozenset())):
        return None
    return match


def located_parcel_match(enrichment_data: Any) -> str | None:
    """'exact', 'street_only' or 'address_point' for a showable located PIN, else None."""
    return _matched_tier(enrichment_data, _SHOWN_MATCHES_BY_SOURCE)


def located_parcel_id(enrichment_data: Any, *, exact_only: bool = False) -> str | None:
    """The located King PIN for a showable match, else None.

    ``exact_only`` narrows to street + ZIP matches, for anything that spends money.
    """
    match = located_parcel_match(enrichment_data)
    if match is None or (exact_only and match != MATCH_EXACT):
        return None
    return enrichment_data["kc_pin"]


def mailing_lookup_pin(enrichment_data: Any) -> str | None:
    """The located King PIN whose extract or tax-bill mailing may be written, else None."""
    if _matched_tier(enrichment_data, _MAILING_MATCHES_BY_SOURCE) is not None:
        return enrichment_data["kc_pin"]
    # A strict point match stored before tiers existed (no kc_pin_match, and a point or
    # absent source) has always been eligible for mailing; only the address-point tiers
    # are new, and they always carry both keys.
    if (isinstance(enrichment_data, dict)
            and enrichment_data.get("kc_pin_source", KING_GIS_POINT_SOURCE) == KING_GIS_POINT_SOURCE
            and enrichment_data.get("kc_pin_status") == "matched"
            and "kc_pin_match" not in enrichment_data):
        pin = enrichment_data.get("kc_pin")
        if isinstance(pin, str) and len(pin) == 10 and pin.isdigit():
            return pin
    return None


def parcel_source_label(enrichment_data: Any) -> str:
    """Provenance text for a located parcel, '' when none is shown."""
    return _LABELS.get(located_parcel_match(enrichment_data) or "", "")
