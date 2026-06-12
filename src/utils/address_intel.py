"""Owner-location intelligence: absentee + out-of-state flags from the two addresses.

Gap-analysis Tier 0 (2026-06-12, Codex-consulted). These are the single most-used
investor filters and are derivable from data we already store: the property
(situs) address and the owner's mailing address. STORED columns (migration 057),
NOT generated columns — address parsing is too business-rule-heavy for an
IMMUTABLE SQL expression (Codex). This is the one shared helper; the worker calls
it at insert AND at the end-of-job recompute (after enrichment fills mailing),
and the backfill calls it for existing rows, so the logic can never drift.

Absentee definition (Codex): compare the NORMALIZED PARSED components, not raw
strings. Base street (unit stripped, suffix/directional canonicalized) + ZIP are
the deliverable-location discriminators; a unit-only difference where the base
street and ZIP match is NOT absentee (county situs routinely omits the unit the
mailing carries — marking those absentee would be noisy false positives). NULL
(unknown) when either address is missing — never guess False.
"""
from __future__ import annotations

import re

from src.utils.lead_formatting import parse_property_for_display

# Trailing secondary-unit designator → stripped before comparing the base street.
_UNIT_STRIP_RE = re.compile(
    r"\b(?:#|APT|APARTMENT|UNIT|STE|SUITE|BLDG|BLD|FL|FLR|FLOOR|RM|ROOM|"
    r"SPC|SPACE|LOT|TRLR|TRAILER|DEPT|NO|BOX|PO\s+BOX)\b.*$",
    re.IGNORECASE,
)
# "#5" (no space) form, plus any leftover stray hash.
_HASH_UNIT_RE = re.compile(r"#\s*\w+.*$")
_PUNCT_RE = re.compile(r"[.,]+")
_WS_RE = re.compile(r"\s+")

# Common USPS street-suffix + directional aliases → a canonical token, so
# "123 MAIN STREET" and "123 MAIN ST" don't read as two different places.
_SUFFIX = {
    "STREET": "ST", "STR": "ST", "AVENUE": "AVE", "AV": "AVE", "ROAD": "RD",
    "DRIVE": "DR", "LANE": "LN", "BOULEVARD": "BLVD", "BLVD": "BLVD",
    "COURT": "CT", "PLACE": "PL", "TERRACE": "TER", "TERR": "TER",
    "CIRCLE": "CIR", "PARKWAY": "PKWY", "PKY": "PKWY", "HIGHWAY": "HWY",
    "TRAIL": "TRL", "WAY": "WAY", "LOOP": "LOOP", "PLAZA": "PLZ",
    "SQUARE": "SQ", "CRESCENT": "CRES",
}
_DIRECTIONAL = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}


def _normalize_street(street: str | None) -> str:
    """Canonical comparable street: upper, unit-stripped, suffix/dir-canonical."""
    if not street:
        return ""
    s = _HASH_UNIT_RE.sub("", street)
    s = _UNIT_STRIP_RE.sub("", s)
    s = _PUNCT_RE.sub(" ", s).upper()
    s = _WS_RE.sub(" ", s).strip()
    tokens = [_SUFFIX.get(t, _DIRECTIONAL.get(t, t)) for t in s.split(" ")]
    return " ".join(tokens).strip()


def _normalize_full(addr: str) -> str:
    s = _PUNCT_RE.sub(" ", addr).upper()
    return _WS_RE.sub(" ", s).strip()


def _zip5(z: str | None) -> str:
    return (z or "")[:5]


def _addresses_differ(property_address: str, mailing_address: str) -> bool:
    """True when the owner's mailing location clearly differs from the property.

    Both args are non-empty (caller guarantees). Component compare per Codex:
    base street first; then ZIP if both have one; else city/state; unit-only
    differences fall through to NOT-different. Falls back to a normalized
    full-string compare only when neither side parses a usable street.
    """
    p = parse_property_for_display(property_address)
    m = parse_property_for_display(mailing_address)
    p_street = _normalize_street(p["street"])
    m_street = _normalize_street(m["street"])

    if not p_street or not m_street:
        return _normalize_full(property_address) != _normalize_full(mailing_address)
    if p_street != m_street:
        return True

    # Base street matches — discriminate on ZIP, else city/state. Unit-only diffs
    # (situs omits the unit the mailing carries) land here and read as NOT absentee.
    if p["zip"] and m["zip"]:
        return _zip5(p["zip"]) != _zip5(m["zip"])
    p_state, m_state = (p["state"] or ""), (m["state"] or "")
    if p_state and m_state and p_state != m_state:
        return True
    p_city = (p["city"] or "").upper().strip()
    m_city = (m["city"] or "").upper().strip()
    if p_city and m_city and p_city != m_city:
        return True
    return False


def compute_owner_flags(
    property_address: str | None, mailing_address: str | None
) -> dict[str, object]:
    """Derive the four owner-location fields from the two addresses.

    Returns {property_state, owner_state, absentee_owner, out_of_state_owner}.
    `owner_state` is the state parsed from the MAILING address (where the owner
    receives mail — product language calls it the owner's state). Booleans are
    NULL (None) when the inputs can't answer them, so downstream filters must use
    `IS TRUE` / `IS NOT TRUE`, never Python truthiness (Codex): False = known
    not-absentee, None = unknown.
    """
    property_state = parse_property_for_display(property_address)["state"] if property_address else None
    owner_state = parse_property_for_display(mailing_address)["state"] if mailing_address else None

    if owner_state and property_state:
        out_of_state: bool | None = owner_state != property_state
    else:
        out_of_state = None

    if property_address and mailing_address:
        absentee: bool | None = _addresses_differ(property_address, mailing_address)
    else:
        absentee = None

    return {
        "property_state": property_state,
        "owner_state": owner_state,
        "absentee_owner": absentee,
        "out_of_state_owner": out_of_state,
    }
