"""Display-oriented name/address splitting for dialer-friendly CSV export.

DISTINCT from skip_trace.select_traceable_owner / _parse_full_address: those are
tuned to gate PAID skip-trace (conservative — blank for anything ambiguous, to
avoid wasting credits). For a CSV a human imports into a dialer we want the
opposite bias: fill first/last and address parts whenever we can do so WITHOUT
emitting something misleading (Codex review). Two rules hold:

  1. Never invent. If a part can't be parsed confidently, leave THAT field blank
     and rely on the full party_name / property_address columns (kept alongside).
  2. Never put garbage in an authoritative column. A wrong city/state/zip is worse
     than a blank one, because a dialer maps it into structured fields and silently
     corrupts the record. So state must be a real 2-letter US code and zip must be
     a real ZIP before we split them out.

Pure functions over raw strings. The caller sanitizes the OUTPUT for CSV at emit
time (never sanitize before parsing — escaping changes the string shape).
"""
import re

# US state/territory 2-letter codes — gate for splitting out a `state` column so
# a token like a street-type abbreviation can't be mistaken for a state.
_US_STATES = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR", "VI", "GU", "AS", "MP",
})

# Tokens that mark a party_name as a NON-person (entity). If any appears we emit
# no first/last (the full party_name column still carries it).
_ENTITY_TOKENS = frozenset({
    "LLC", "LLP", "LP", "PLLC", "INC", "CORP", "CO", "COMPANY", "TRUST", "TR",
    "BANK", "MORTGAGE", "LOAN", "SERVICING", "ASSOCIATION", "ASSN", "HOA",
    "FUND", "HOLDINGS", "PROPERTIES", "PROPERTY", "INVESTMENTS", "INVESTMENT",
    "CAPITAL", "GROUP", "PARTNERS", "PARTNERSHIP", "ENTERPRISES", "REALTY",
    "HOMES", "CHURCH", "MINISTRIES", "AUTHORITY", "DISTRICT", "DEPARTMENT",
    "NA", "FSB", "FOUNDATION", "SERVICES", "MANAGEMENT", "VENTURES",
})

_ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")
# No-comma tail: "<everything> <ST> <ZIP>". We can confidently lift the trailing
# state+zip, but WITHOUT a comma the street/city boundary is unknowable, so the
# whole pre-state chunk becomes `street` and `city` stays blank (honest > a wrong
# guess — Codex). Accepted only when ST is a real state code and ZIP validates.
_NO_COMMA_TAIL_RE = re.compile(
    r"^(?P<street>.+?)\s+(?P<state>[A-Za-z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$"
)
# "<CITY> <ST> <ZIP>" inside a single comma part (e.g. "SEATTLE WA 98101").
_CITY_STATE_ZIP_RE = re.compile(
    r"^(?P<city>.+?)\s+(?P<state>[A-Za-z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$"
)
# Trailing "<ST> <ZIP?>" or "<ZIP>" in the last comma part.
_STATE_ZIP_RE = re.compile(r"^(?P<state>[A-Za-z]{2})\s*(?P<zip>\d{5}(?:-\d{4})?)?$")

_ESTATE_PREFIX_RE = re.compile(r"^(?:THE\s+)?ESTATE\s+OF\s+", re.IGNORECASE)
_NAME_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")

# Trailing phone extension ('x123', 'ext 5', 'ext: 7', 'x. 7', '#4') — stripped
# before normalizing so a valid 10-digit base isn't lost to merged ext digits.
_PHONE_EXT_RE = re.compile(r"(?i)\s*(?:ext(?:ension)?|x|#)[.:]?\s*\d+\s*$")


def normalize_phone_for_dialer(raw: str | None) -> str:
    """Normalize a phone to bare 10-digit (e.g. '2065551234') for dialer import.

    Bare 10-digit is accepted by every mainstay RE dialer (PhoneBurner, Mojo,
    Kixie, ReadyMode, CallTools, BatchDialer, …); E.164 '+1' is rejected by some
    (PhoneBurner docs say no '+'), so 10-digit is the safest universal default.
    Anything that doesn't resolve to a clean US 10-digit number returns '' — a
    blank cell is predictable; a malformed number can reject/poison a dialer row
    (Codex). Strips a trailing extension first, then all non-digits, then drops a
    US country-code '1' from an 11-digit number.
    """
    if not raw:
        return ""
    digits = re.sub(r"\D", "", _PHONE_EXT_RE.sub("", str(raw).strip()))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""

# Secondary-unit / delivery-line designators — a chunk starting with one of
# these is an apartment/suite/box/rural-route line, NOT a city. Folded into
# street, never emitted as city. (PMB/POB/P.O. BOX/RR/RFD/HC/GENERAL DELIVERY
# added per Codex — postal delivery lines that are digitless yet not cities.)
_UNIT_RE = re.compile(
    r"^(?:#|APT|APARTMENT|UNIT|STE|SUITE|BLDG|BLD|FL|FLR|FLOOR|RM|ROOM|"
    r"SPC|SPACE|LOT|TRLR|TRAILER|DEPT|NO|BOX|PO\s+BOX|P\.?\s?O\.?\s+BOX|"
    r"PMB|POB|RR|RFD|HC|GENERAL\s+DELIVERY)\b",
    re.IGNORECASE,
)

# Street-suffix tokens (abbreviated + unabbreviated) — used ONLY to disambiguate
# the 'NE' directional-vs-Nebraska collision in comma-less addresses: a 2-letter
# 'NE' right after a street suffix is a Seattle-style grid directional
# ('6504 108TH AVE NE 98033'), not a state. NEVER used to guess a street/city
# boundary (that inference stays out — Codex).
_STREET_SUFFIXES = frozenset({
    "AVE", "AVENUE", "ST", "STREET", "RD", "ROAD", "DR", "DRIVE",
    "BLVD", "BOULEVARD", "WAY", "LN", "LANE", "CT", "COURT", "PL", "PLACE",
    "HWY", "HIGHWAY", "TER", "TERRACE", "CIR", "CIRCLE", "LOOP",
    "PKWY", "PARKWAY", "SQ", "SQUARE", "TRL", "TRAIL",
})

# Trailing bare ZIP (no state token): '1420 E PINE ST 98122'. The zip is
# validated-confident on its own; the rest stays street.
_TRAILING_ZIP_RE = re.compile(r"^(?P<street>.+?)\s+(?P<zip>\d{5}(?:-\d{4})?)$")

# Tokens that make a trailing 5-digit number a BOX/UNIT number, not a zip —
# 'PO BOX 98292' must not be read as zip 98292.
_ZIP_LIFT_BLOCKERS = frozenset({
    "BOX", "PMB", "POB", "NO", "APT", "UNIT", "STE", "SUITE", "SPC", "SPACE",
    "LOT", "RM", "ROOM", "TRLR", "#", "RR", "RFD", "HC",
})


def _last_word(chunk: str) -> str:
    """Uppercased, punctuation-stripped final token ('AVE.' -> 'AVE'); '' if none."""
    toks = chunk.split()
    return re.sub(r"[^A-Za-z#]", "", toks[-1]).upper() if toks else ""
# Surname particles (compound last names): keep them attached to the surname so
# 'VAN DYKE JOHN' -> last 'VAN DYKE', not 'VAN'.
_SURNAME_PARTICLES = frozenset({
    "VAN", "VON", "DE", "DEL", "DELA", "LA", "LE", "DI", "DA", "DU", "DOS",
    "MC", "MAC", "O", "ST", "SAINT", "SANTA", "SAN",
})


def _is_entity(name: str) -> bool:
    """True when the name contains an entity marker or a digit (not a person)."""
    upper = name.upper()
    if any(ch.isdigit() for ch in upper):
        return True
    tokens = {re.sub(r"[^A-Z]", "", t) for t in upper.split()}
    return bool(tokens & _ENTITY_TOKENS)


def _person_first_last(name: str, recorder_order: bool) -> tuple[str | None, str | None]:
    """Split one cleaned person name into (first, last).

    recorder_order=True  -> tokens are 'LAST FIRST [MIDDLE]' (WA county recorder).
    recorder_order=False -> tokens are 'FIRST [MIDDLE] LAST' (natural order, e.g.
                            after stripping an 'ESTATE OF ' prefix).
    """
    if "," in name:
        # Comma always means 'LAST, FIRST [MIDDLE]' regardless of recorder_order.
        # The FULL pre-comma chunk is the surname (handles 'DE LA CRUZ, MARIA').
        last_part, _, rest = name.partition(",")
        last_toks = _NAME_TOKEN_RE.findall(last_part)
        first_toks = _NAME_TOKEN_RE.findall(rest)
        last = " ".join(last_toks) if last_toks else None
        first = first_toks[0] if first_toks else None
        return first, last

    toks = _NAME_TOKEN_RE.findall(name)
    if not toks:
        return None, None
    if len(toks) == 1:
        return None, toks[0]  # lone token -> treat as a surname, first blank
    if recorder_order:
        # 'LAST FIRST [MIDDLE]'. A run of leading surname particles binds to the
        # surname ('VAN DYKE JOHN' -> 'VAN DYKE'/JOHN; 'DE LA CRUZ MARIA' ->
        # 'DE LA CRUZ'/MARIA). Only for 3+ tokens so a plain 'VAN JOHN' (LAST
        # FIRST) is untouched.
        if len(toks) >= 3 and toks[0].upper() in _SURNAME_PARTICLES:
            i = 0
            while i < len(toks) - 1 and toks[i].upper() in _SURNAME_PARTICLES:
                i += 1
            first = toks[i + 1] if i + 1 < len(toks) else None
            return first, " ".join(toks[: i + 1])
        return toks[1], toks[0]
    # natural order 'FIRST [MIDDLE] LAST' (e.g. after stripping 'ESTATE OF ')
    return toks[0], toks[-1]


def split_owner_for_display(party_name: str | None) -> tuple[str | None, str | None]:
    """Best-effort (first_name, last_name) for a dialer CSV — permissive but honest.

    Returns (None, None) for entities/unparseable names; the caller always keeps the
    full party_name column. Among ' / '-separated owners, the first person-looking
    candidate wins (so a real person beside a bank/trustee is still surfaced).
    """
    if not party_name or not party_name.strip():
        return None, None

    for raw in (party_name.split(" / ") or [party_name]):
        cand = raw.strip()
        if not cand:
            continue
        estate = bool(_ESTATE_PREFIX_RE.match(cand))
        if estate:
            cand = _ESTATE_PREFIX_RE.sub("", cand).strip()
        if not cand or _is_entity(cand):
            continue
        # 'ESTATE OF JOHN SMITH' is written in natural order; plain recorder rows
        # are 'LAST FIRST'. A comma form is handled inside _person_first_last.
        first, last = _person_first_last(cand, recorder_order=not estate)
        if first or last:
            return first, last
    return None, None


# King assessor "Name" cell joins co-owners with '+' ("JANNETTO RUSSELL D+GINA L")
# and occasionally '/', '&', ';', ' AND '. Split so each owner is classified on its own.
_OWNER_SEP_RE = re.compile(r"\s*\+\s*|\s*/\s*|\s+&\s+|\s*;\s*|\s+AND\s+", re.IGNORECASE)


def _surname_key(surname: str | None) -> str:
    """Alpha-only uppercase surname key. Strips hyphens/punctuation so a REMARRIED or
    new owner reads as different: 'JONES-MITCHELL' -> 'JONESMITCHELL' != 'JONES' (a real
    title move), while 'BOUCHER' == 'BOUCHER' (same person, abbreviated first name)."""
    return re.sub(r"[^A-Z]", "", (surname or "").upper())


def classify_probate_title_status(
    party_name: str | None, current_owner: str | None
) -> str:
    """Conservative, NON-authoritative signal comparing a probate lead's party_name
    (the deceased on the death certificate) to the Assessor's CURRENT owner/taxpayer.

    Returns one of:
      - "current_owner_entity_or_trust" : title is held by a trust/LLC/estate entity
        (any owner segment is an entity) — a common estate-planning / post-death signal.
      - "current_owner_name_differs"    : the deceased's surname matches NONE of the
        current owner's surnames — the person on title is (probably) a different party.
      - ""                              : same surname on title (deceased or a same-name
        heir/spouse still holds it), or nothing parseable to compare.

    Deliberately humble: it is a scan aid, not proof of a transfer (that needs the deed
    chain). The raw current_owner is the authoritative value the user reads. Entity wins
    over surname; a surname match anywhere suppresses the "differs" flag so a surviving
    co-owner or the deceased's own estate never false-flags (Codex).
    """
    if not current_owner or not current_owner.strip():
        return ""
    # Strip each segment up front: a leading/trailing space would make
    # `stripped == seg` below false and flip recorder_order, mis-reading the surname
    # (" HOWTON JAMES W " -> surname "W", a false name_differs) (Codex).
    segments = [s.strip() for s in _OWNER_SEP_RE.split(current_owner) if s.strip()]
    if any(_is_entity(seg) for seg in segments):
        return "current_owner_entity_or_trust"
    _, dec_last = split_owner_for_display(party_name)
    dec_key = _surname_key(dec_last)
    if not dec_key:
        return ""  # deceased name is an entity/unparseable — nothing to compare
    owner_keys: set[str] = set()
    for seg in segments:
        stripped = _ESTATE_PREFIX_RE.sub("", seg).strip()
        _, last = _person_first_last(stripped, recorder_order=(stripped == seg))
        key = _surname_key(last)
        if key:
            owner_keys.add(key)
    if owner_keys and dec_key not in owner_keys:
        return "current_owner_name_differs"
    return ""


def parse_property_for_display(addr: str | None) -> dict:
    """Split a property address into {street, city, state, zip} for a CSV.

    Validated: a `state` is only emitted when it is a real 2-letter US code and a
    `zip` only when it matches a ZIP pattern. When the city/state/zip can't be read
    confidently they stay None and the caller keeps the full property_address.
    Always returns `street` (the whole string if nothing else parses). NEVER falls
    back to mailing_address — these columns mean the PROPERTY address (Codex).
    """
    out = {"street": None, "city": None, "state": None, "zip": None}
    if not addr or not addr.strip():
        return out
    clean = addr.strip().rstrip(",").strip()

    if "," not in clean:
        m = _NO_COMMA_TAIL_RE.match(clean)
        if m and m.group("state").upper() in _US_STATES and _ZIP_RE.match(m.group("zip")):
            head = m.group("street").strip()
            state = m.group("state").upper()
            if state == "NE" and _last_word(head) in _STREET_SUFFIXES:
                # Directional/Nebraska collision: 'NE' right after a street
                # suffix is a grid directional ('6504 108TH AVE NE 98033'), not
                # a state. Fold it back into street, lift only the validated
                # zip, and leave state blank — never guess the real one.
                # ('123 MAIN ST OMAHA NE 68102' keeps NE: OMAHA is no suffix.)
                out["street"] = f"{head} {m.group('state')}"
                out["zip"] = m.group("zip")
                return out
            if not any(ch.isdigit() for ch in head) and not _UNIT_RE.match(head):
                # A digitless chunk before 'ST ZIP' is a CITY, not a street —
                # street lines carry a house number or box digits. Snohomish
                # tax mailing addresses are city-only ('STANWOOD WA 98292');
                # emitting them as street put a city in the street column.
                out["city"] = head
                out["state"] = state
                out["zip"] = m.group("zip")
                return out
            # state+zip are confident; city is unknowable without a comma -> blank.
            out["street"] = head or None
            out["state"] = state
            out["zip"] = m.group("zip")
            return out
        m2 = _TRAILING_ZIP_RE.match(clean)
        if m2 and _ZIP_RE.match(m2.group("zip")) and (
            _last_word(m2.group("street")) not in _ZIP_LIFT_BLOCKERS
        ):
            # Trailing bare ZIP, no state token ('1420 E PINE ST 98122',
            # '27323 218TH AVE SE 98038' — SE/SW/NW are not state codes). The
            # zip validates on its own; the rest stays street. Blocked when the
            # preceding token makes the number a box/unit number, not a zip.
            out["street"] = m2.group("street").strip() or None
            out["zip"] = m2.group("zip")
            return out
        out["street"] = clean or None
        return out

    parts = [p for p in (x.strip() for x in clean.split(",")) if p]
    if not parts:
        return out

    # tail = everything after the street; pull state+zip off its END, then the
    # part immediately before them is the city candidate.
    tail = parts[1:]
    state, zip_, tail = _strip_state_zip(tail)

    city = None
    if tail:
        cand = tail[-1]
        # A real city has no digits and is not a unit/suite line. Anything that
        # fails (e.g. 'APT 4', 'UNIT 2', a leftover bad state token) folds into
        # street instead of becoming a bogus authoritative city column (Codex).
        if not _UNIT_RE.match(cand) and not any(ch.isdigit() for ch in cand):
            city = cand
            tail = tail[:-1]

    # street = the street part + any leftover tail fragments (units, etc.).
    street_bits = [parts[0]] + tail
    out["street"] = ", ".join(b for b in street_bits if b) or None
    out["city"] = city
    out["state"] = state
    out["zip"] = zip_
    return out


def _strip_state_zip(tail: list[str]) -> tuple[str | None, str | None, list[str]]:
    """Pull a validated trailing (state, zip) off the comma-part tail.

    Returns (state, zip, remaining_parts). Handles the part being a bare ZIP, a
    bare STATE, 'ST ZIP', or 'CITY ST ZIP' (the inline city is pushed back onto
    the remaining parts so the caller can treat it as the city candidate). State
    is only accepted as a real 2-letter US code; zip only when it validates.
    """
    if not tail:
        return None, None, tail
    parts = list(tail)
    state = zip_ = None
    last = parts[-1]

    if _ZIP_RE.match(last):
        zip_ = last
        parts.pop()
        if parts and parts[-1].strip().upper() in _US_STATES:
            state = parts[-1].strip().upper()
            parts.pop()
        return state, zip_, parts

    m = _CITY_STATE_ZIP_RE.match(last)  # "CITY ST ZIP" jammed in one part
    if m and m.group("state").upper() in _US_STATES:
        state = m.group("state").upper()
        zip_ = m.group("zip")
        parts.pop()
        inline_city = m.group("city").strip()
        if inline_city:
            parts.append(inline_city)
        return state, zip_, parts

    m2 = _STATE_ZIP_RE.match(last)  # "ST" or "ST ZIP"
    if m2 and m2.group("state").upper() in _US_STATES:
        state = m2.group("state").upper()
        if m2.group("zip"):
            zip_ = m2.group("zip")
        parts.pop()
    return state, zip_, parts
