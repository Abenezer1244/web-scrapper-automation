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
# The lookahead requires a real unit identifier (a number, '#', or a 1-2 char
# token like 'B') AFTER the designator, so ambiguous words that are also street
# names — 'BOX CANYON RD', 'NO NAME RD' — are NOT eaten (Codex review): there the
# token is followed by a multi-letter word, not a unit id, so the strip skips it.
_UNIT_STRIP_RE = re.compile(
    r"\b(?:#|APT|APARTMENT|UNIT|STE|SUITE|BLDG|BLD|FL|FLR|FLOOR|RM|ROOM|"
    r"SPC|SPACE|LOT|TRLR|TRAILER|DEPT|NO|BOX|PO\s+BOX)\b\s*(?=[#\d]|\w{1,2}\b).*$",
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


# Tokens a county situs may legitimately OMIT at the end of a street while the
# owner's mailing address carries them: the canonical suffixes and directionals
# produced by _normalize_street (values of _SUFFIX / _DIRECTIONAL).
_TRAILING_OPTIONAL_TOKENS = frozenset(_SUFFIX.values()) | frozenset(_DIRECTIONAL.values())


def _same_street_modulo_trailing_tokens(a: str, b: str) -> bool:
    """True when the two normalized streets differ ONLY by trailing suffix /
    post-directional tokens present on one side and absent on the other.

    Pierce County GIS `Site_Address` routinely drops the suffix or the
    post-directional that the same parcel's `Delivery_Address` keeps
    ("20508 ISLAND PKWY" vs "20508 ISLAND PKWY E", "1006 S 34TH" vs
    "1006 S 34TH ST"). Those are the same base street, so the caller must fall
    through to the ZIP/city discriminators (→ None when the situs has none)
    instead of calling the owner absentee. Deliberately TRAILING-only: a
    differing house number, a dropped LEADING directional ("E MAIN" vs "MAIN"),
    or any non-suffix extra token still reads as a different street.
    """
    ta, tb = a.split(" "), b.split(" ")
    if len(ta) == len(tb):
        return False
    short, long = (ta, tb) if len(ta) < len(tb) else (tb, ta)
    if long[: len(short)] != short:
        return False
    return all(tok in _TRAILING_OPTIONAL_TOKENS for tok in long[len(short):])


def _street_absorbed_city(p: dict, m: dict, p_street: str, m_street: str) -> bool:
    """True when one side's STREET is the other's street with that other side's own
    CITY glued onto the end.

    An NTS notice prints the situs with no comma before the city — "Commonly known
    as: 1207 118TH PL SW EVERETT, WASHINGTON 98204-4813" — so the display parser,
    which splits on commas, reads the street as "1207 118TH PL SW EVERETT" while the
    owner's mailing address parses cleanly to street "1207 118TH PL SW" + city
    "EVERETT". Same place, two different street strings.

    Until now that shape only survived by accident: `_addresses_differ` returns False
    early when the two FULL strings normalize identically, which held whenever both
    sides carried the same ZIP spelling, and broke the moment one side had ZIP+4 and
    the other ZIP5 — producing a confident absentee_owner=True for an owner living in
    the house. Measured in production 2026-09-03: 1 such row out of 2,432 absentee
    rows (a Snohomish trustee-sale lead), so this is narrow by design, matched on the
    other side's OWN parsed city rather than on any city-looking token.

    This only defers the verdict — the caller still has to CONFIRM same-place with a
    matching ZIP (or city+state) before returning False. Different ZIPs still read as
    absentee, so a genuinely different property cannot be merged by this.

    Two guards keep it to the shape it was written for (Codex review):
      * the absorbing side must have parsed NO city of its own. A city glued to the
        street is exactly why the parser found none; if it DID find one, the trailing
        word is part of the street name and dropping it would compare two real streets
        as equal.
      * the other side's street must contain a letter. Otherwise "123, EVERETT, WA"
        (a bare house number, city, state) reads as street "123" + city "EVERETT" and
        would absorb into any "123 <CITY>" street in the same ZIP.
    """
    for street, other_street, other, own in (
        (p_street, m_street, m, p), (m_street, p_street, p, m)
    ):
        if own.get("city"):
            continue
        city = _normalize_street(other.get("city"))
        if not city or not other_street:
            continue
        if not any(c.isalpha() for c in other_street):
            continue
        if street == f"{other_street} {city}":
            return True
    return False


def _normalize_full(addr: str) -> str:
    s = _PUNCT_RE.sub(" ", addr).upper()
    return _WS_RE.sub(" ", s).strip()


def _zip5(z: str | None) -> str:
    return (z or "")[:5]


# County bulk files encode "no data" as a WORD, not as blank: the Snohomish tax
# feed ships a situs street of literal UNKNOWN tokens ("UNKNOWN UNKNOWN, GRANITE
# FALLS WA 98252"). Measured in production 2026-09-03: 408 such rows, every one of
# them with the ENTIRE street segment made of UNKNOWN tokens, and 0 rows where
# UNKNOWN appears inside an otherwise real street — so anchoring on the whole
# segment cannot swallow a genuine address like "123 UNKNOWN RD".
# ONLY the token that was actually measured. UNAVAILABLE / NONE / N/A were
# considered and REJECTED: 0 occurrences in production, and inventing an
# unmeasured guard on this connector is precisely the mistake that once
# false-aborted 14.79% of real Snohomish rows. Add one only after measuring it.
_PLACEHOLDER_STREET_RE = re.compile(r"UNKNOWN", re.IGNORECASE)


def street_is_placeholder(addr: str | None) -> bool:
    """True when an address's STREET segment is nothing but placeholder tokens.

    Only the street is judged: in "UNKNOWN UNKNOWN, GRANITE FALLS WA 98252" the
    locality is REAL and still supports property_state / out_of_state_owner. It is
    absentee_owner alone that cannot be known, because that one compares streets.
    """
    if not addr:
        return False
    street = addr.split(",")[0].strip()
    if not street:
        return False
    # Tokenise on WORD characters, not whitespace: splitting on spaces alone left
    # punctuated debris ("UNKNOWN.", "UNKNOWN-UNKNOWN", "UNKNOWN/UNKNOWN") failing
    # fullmatch, so a fabricated street walked through and kept a confident
    # absentee_owner (Codex). Measured 0 such rows in prod today, so this changes
    # nothing now — it is a cheap guard against the next shape the county emits.
    # Still `all(...)`, so a REAL street merely containing the word ("123 UNKNOWN
    # RD") is never suppressed.
    tokens = re.findall(r"[A-Za-z0-9]+", street)
    return bool(tokens) and all(_PLACEHOLDER_STREET_RE.fullmatch(t) for t in tokens)


def _addresses_differ(property_address: str, mailing_address: str) -> bool | None:
    """Tri-state: True=clearly different, False=confirmed same, None=underdetermined.

    Both args are non-empty (caller guarantees). Component compare per Codex:
    base street first; if it differs → True. If it matches, return False ONLY when
    a discriminator POSITIVELY confirms the same location (matching ZIP, or
    matching city+state) — otherwise None (unknown), because counties here emit
    street-only property addresses and "same street, nothing else to compare" is
    not proof of owner-occupancy. Falls back to a normalized full-string compare
    only when neither side parses a usable street.
    """
    # A placeholder street is NOT a street. "UNKNOWN UNKNOWN" can never equal a
    # real mailing street, so without this the comparator returns a CONFIDENT
    # absentee=True for a property whose address we simply do not have — exactly
    # the fabricated signal the house rule forbids. Unknown means None.
    if street_is_placeholder(property_address) or street_is_placeholder(mailing_address):
        return None

    # Byte-identical (post-normalize) is unambiguous same-place → confirmed False,
    # even with no parsed ZIP/city/state to discriminate.
    if _normalize_full(property_address) == _normalize_full(mailing_address):
        return False

    p = parse_property_for_display(property_address)
    m = parse_property_for_display(mailing_address)
    p_street = _normalize_street(p["street"])
    m_street = _normalize_street(m["street"])

    if not p_street or not m_street:
        # No parsed street on a side — fall back to a whole-string compare. Equal
        # strings are confirmed-same (False); different strings are different (True).
        return _normalize_full(property_address) != _normalize_full(mailing_address)
    if (
        p_street != m_street
        and not _same_street_modulo_trailing_tokens(p_street, m_street)
        and not _street_absorbed_city(p, m, p_street, m_street)
    ):
        return True

    # Base street matches — need a positive discriminator to call it same vs diff.
    if p["zip"] and m["zip"]:
        return _zip5(p["zip"]) != _zip5(m["zip"])  # ZIP is decisive both ways
    p_state, m_state = (p["state"] or ""), (m["state"] or "")
    if p_state and m_state and p_state != m_state:
        return True  # same street text, different state → different place
    p_city = (p["city"] or "").upper().strip()
    m_city = (m["city"] or "").upper().strip()
    if p_city and m_city and p_state and m_state:
        return p_city != m_city  # same state: city confirms same/diff
    # Same street, but no ZIP and not enough city/state to confirm → unknown.
    return None


def address_match_key(address: str | None) -> str | None:
    """A normalized comparable key for deciding two addresses are the same property.

    `<normalized base street>|<zip5>` (or just the street when no ZIP parses). Used
    by the NTS pipeline: the crawler stores it on each notice and the matcher
    computes the SAME key for a lead's property_address, so identical keys = the
    same physical property regardless of source formatting (suffix spelling, unit,
    directional). Returns None when no usable street parses (don't match on junk).
    """
    if not address:
        return None
    parsed = parse_property_for_display(address)
    street = _normalize_street(parsed["street"])
    if not street:
        return None
    zip5 = (parsed["zip"] or "")[:5]
    return f"{street}|{zip5}" if zip5 else street


def compose_situs(
    property_address: str | None,
    property_city: str | None = None,
    property_state: str | None = None,
    property_zip: str | None = None,
) -> str | None:
    """The street-only situs plus its structured parts as one comparable line.

    Migration 085 stores city/state/zip BESIDE the frozen street-only
    property_address; the comparator works on address strings, so rebuild the
    full line here. Parts are appended only when present — nothing is guessed.

    CANONICAL, NOT APPEND-ONLY: `property_address` is street-only on the GIS path
    but a FULL situs line on others — the King assessor's Site Address carries its
    own trailing ZIP ("2019 SW 318TH PL 4C 98023"), and
    scripts/backfill_property_situs_parts.py parses its parts straight out of the
    line and hands that same line back here. Blind appending gave those a second
    copy of their own tail ("… 4C 98023, 98023"), which pushed the ZIP into the
    parsed STREET, so the street no longer matched the mailing street and
    `absentee_owner` flipped False -> True on owner-occupied leads.

    So: parse the line first, and let whatever it ALREADY carries win; the
    structured parts only fill what is genuinely absent. Rebuilding from the
    parsed street also fixes ordering (a supplied city can never land after a ZIP
    the line already had) and makes a line/part CONFLICT resolve to the line's own
    value rather than to an invented "…, WA 99999, TACOMA, WA 98402" — a conflict
    is therefore NOT surfaced here; if that ever needs to read as "unknown" it
    belongs in compute_owner_flags, not in a string builder (Codex).

    Idempotent for migration-085-shaped input (a situs line + scalar city/state/zip);
    that is the only way the one production caller uses it, and it is not a promise
    about arbitrary strings — a part containing its own commas can still re-parse
    differently on a second pass (Codex).

    KNOWN PARSER EDGE (not introduced here, and not present in live data): a street
    whose suffix is itself a 2-letter state code — "1407 KAYE WY 82901", WY as an
    abbreviation of WAY — parses as state=WY and loses the suffix from `street`.
    Same family as the NE directional collision already handled in
    parse_property_for_display. Measured 2026-09-03: 0 of 23,284 production rows
    parse to a non-WA state, and absentee is True both before and after this change,
    so it is documented rather than guessed at.
    """
    if not property_address:
        return property_address
    line = property_address.strip()
    if not (property_city or property_state or property_zip):
        return line
    # parse_property_for_display only emits a VALIDATED state (real 2-letter code)
    # and zip, so "98023 MAIN ST" yields no zip and "123 LAKE ST" no city — a bare
    # substring test would have mistaken both.
    have = parse_property_for_display(line)
    street = (have["street"] or line).strip()
    city = have["city"] or (property_city.strip() if property_city else None)
    state = have["state"] or (property_state.strip() if property_state else None)
    zipc = have["zip"] or (property_zip.strip() if property_zip else None)

    out = street
    if city:
        out += f", {city}"
    tail = " ".join(x for x in (state, zipc) if x)
    if tail:
        out += f", {tail}"
    return out


def compute_owner_flags(
    property_address: str | None,
    mailing_address: str | None,
    *,
    property_city: str | None = None,
    property_state: str | None = None,
    property_zip: str | None = None,
) -> dict[str, object]:
    """Derive the four owner-location fields from the two addresses.

    Returns {property_state, owner_state, absentee_owner, out_of_state_owner}.
    `owner_state` is the state parsed from the MAILING address (where the owner
    receives mail — product language calls it the owner's state). Booleans are
    NULL (None) when the inputs can't answer them, so downstream filters must use
    `IS TRUE` / `IS NOT TRUE`, never Python truthiness (Codex): False = known
    not-absentee, None = unknown.

    The keyword parts are the structured situs (migration 085). When given they
    are composed onto the street-only property_address so a same-street mailing
    can be CONFIRMED same-place (real False) by city/zip; without them behaviour
    is exactly as before (strictly opt-in, Codex).
    """
    if property_city or property_state or property_zip:
        property_address = compose_situs(
            property_address, property_city, property_state, property_zip
        )
    # Keep the caller's structured state before the name is rebound below.
    _given_state = (property_state or "").strip().upper()
    property_state = parse_property_for_display(property_address)["state"] if property_address else None
    if not property_state and re.fullmatch(r"[A-Z]{2}", _given_state):
        # No STREET, so compose_situs returned the (empty) address unchanged and the
        # structured parts were discarded — which silently threw away a state we were
        # handed. That is the vacant / raw-land case: King's GIS matches the parcel and
        # gives city+state+ZIP but no ADDR_FULL (~1/3 of its delinquent parcels), so
        # `property_address` stays NULL on purpose (skip trace bills off it). The state
        # is still a REAL source value, and out_of_state_owner only needs the two
        # states — so honour it. absentee_owner stays None below, because comparing
        # streets is still impossible and that is the honest answer (Codex).
        property_state = _given_state
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
