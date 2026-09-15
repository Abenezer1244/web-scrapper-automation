"""The parcel a lead was LOCATED on, when that location is strict enough to show.

Seattle SDCI code violations carry no parcel number. `king_parcel_locate` finds the
King County parcel under the complaint's coordinates and stores it beside the lead as
`enrichment_data.kc_pin`, never in `results.parcel_id`: parcel_id feeds the frozen
billing dedup_hash, and both same-run sibling collapse and enrichment reuse recompute
that hash from the CURRENT parcel_id, so writing a PIN there would bill a property twice.

This module is the single read-side rule for showing that PIN as a Parcel ID (API and
export) and for naming its owner. Two tiers qualify (owner decisions 2026-09-14):
  * exact: one parcel polygon under the point, the same normalized street including
    house number, and the lead's ZIP equal to the parcel's ZIP;
  * street_only: the same, but the source row carried no ZIP to compare (5/5 verified).
A condominium complex parcel never qualifies. Paid skip trace accepts EXACT only
(Codex P1): a missing ZIP is a confidence downgrade not worth a charge.
"""
from __future__ import annotations

from typing import Any

KING_GIS_POINT_SOURCE = "king_gis_point_in_parcel"
MATCH_EXACT = "exact"
MATCH_STREET_ONLY = "street_only"
MATCH_CONDO_COMPLEX = "condo_complex"
PARCEL_SOURCE_LABEL = "County parcel map match"
PARCEL_SOURCE_LABEL_STREET_ONLY = "County parcel map match (street only, no ZIP in source)"
_SHOWN_MATCHES = frozenset({MATCH_EXACT, MATCH_STREET_ONLY})


def located_parcel_match(enrichment_data: Any) -> str | None:
    """'exact' or 'street_only' for a showable located PIN, else None."""
    if not isinstance(enrichment_data, dict):
        return None
    pin = enrichment_data.get("kc_pin")
    if not (isinstance(pin, str) and len(pin) == 10 and pin.isdigit()):
        return None
    match = enrichment_data.get("kc_pin_match")
    if (enrichment_data.get("kc_pin_status") != "matched"
            or enrichment_data.get("kc_pin_source") != KING_GIS_POINT_SOURCE
            # isinstance first: a malformed list/object value is unhashable (Codex P2).
            or not isinstance(match, str) or match not in _SHOWN_MATCHES):
        return None
    return match


def located_parcel_id(enrichment_data: Any, *, exact_only: bool = False) -> str | None:
    """The located King PIN for a showable match, else None.

    ``exact_only`` narrows to street + ZIP matches, for anything that spends money.
    """
    match = located_parcel_match(enrichment_data)
    if match is None or (exact_only and match != MATCH_EXACT):
        return None
    return enrichment_data["kc_pin"]


def parcel_source_label(enrichment_data: Any) -> str:
    """Provenance text for a located parcel, '' when none is shown."""
    match = located_parcel_match(enrichment_data)
    if match == MATCH_EXACT:
        return PARCEL_SOURCE_LABEL
    if match == MATCH_STREET_ONLY:
        return PARCEL_SOURCE_LABEL_STREET_ONLY
    return ""
