"""The parcel a lead was LOCATED on, when that location is strict enough to show.

Seattle SDCI code violations carry no parcel number. `king_parcel_locate` finds the
King County parcel under the complaint's coordinates and stores it beside the lead as
`enrichment_data.kc_pin`, never in `results.parcel_id`: parcel_id feeds the frozen
billing dedup_hash, and both same-run sibling collapse and enrichment reuse recompute
that hash from the CURRENT parcel_id, so writing a PIN there would bill a property twice.

This module is the single read-side rule for showing that PIN as a Parcel ID (API and
export). Only an EXACT match qualifies (owner decision 2026-09-14): one parcel polygon
under the point, the same normalized street including house number, and the lead's ZIP
equal to the parcel's ZIP. A street-only match (the source gave no ZIP) stays unshown.
"""
from __future__ import annotations

from typing import Any

KING_GIS_POINT_SOURCE = "king_gis_point_in_parcel"
MATCH_EXACT = "exact"
MATCH_STREET_ONLY = "street_only"
MATCH_CONDO_COMPLEX = "condo_complex"
PARCEL_SOURCE_LABEL = "County parcel map match"


def located_parcel_id(enrichment_data: Any) -> str | None:
    """The located King PIN when the match was exact, else None."""
    if not isinstance(enrichment_data, dict):
        return None
    pin = enrichment_data.get("kc_pin")
    if not (isinstance(pin, str) and len(pin) == 10 and pin.isdigit()):
        return None
    if (enrichment_data.get("kc_pin_status") != "matched"
            or enrichment_data.get("kc_pin_source") != KING_GIS_POINT_SOURCE
            or enrichment_data.get("kc_pin_match") != MATCH_EXACT):
        return None
    return pin
