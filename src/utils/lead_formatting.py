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
# PUBLIC: skip_trace validates against the same vocabulary, so a paid trace can
# never be sent a fabricated state ('SE' off SEATTLE, 'PK', a 'WS' source typo).
US_STATES = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR", "VI", "GU", "AS", "MP",
    "AA", "AE", "AP",  # USPS military (APO/FPO/DPO) "states"
})
_US_STATES = US_STATES  # legacy private alias (this module's own references)

# A trailing country marks a NON-US address. Its parts are not in a US shape, so
# nothing is split out: the whole text stays in street (the caller keeps the full
# address column too). Before this, '..., TORONTO ON M6K3P1, CANADA' emitted
# city='CANADA'. Deliberately a short list of countries seen on owner mailing
# addresses; the Canadian postal-code check below catches Canada without the name.
FOREIGN_COUNTRIES = frozenset({
    "CANADA", "MEXICO", "UNITED KINGDOM", "UK", "GREAT BRITAIN", "ENGLAND", "SCOTLAND",
    "WALES", "IRELAND", "AUSTRALIA", "NEW ZEALAND", "JAPAN", "CHINA", "HONG KONG",
    "TAIWAN", "KOREA", "SOUTH KOREA", "REPUBLIC OF KOREA", "PHILIPPINES", "VIETNAM",
    "THAILAND", "SINGAPORE", "MALAYSIA", "INDONESIA", "INDIA", "ISRAEL", "GERMANY",
    "FRANCE", "ITALY", "SPAIN", "PORTUGAL", "NETHERLANDS", "BELGIUM", "SWITZERLAND",
    "AUSTRIA", "SWEDEN", "NORWAY", "DENMARK", "FINLAND", "POLAND", "RUSSIA", "UKRAINE",
    "BRAZIL", "ARGENTINA", "CHILE", "COLOMBIA", "PERU", "SOUTH AFRICA", "NIGERIA",
    "KENYA", "ETHIOPIA", "EGYPT", "UNITED ARAB EMIRATES", "UAE", "SAUDI ARABIA",
})
_FOREIGN_COUNTRIES = FOREIGN_COUNTRIES  # legacy private alias (this module's references)
_US_COUNTRY_TAILS = frozenset({"USA", "US", "U S A", "UNITED STATES", "UNITED STATES OF AMERICA"})
# Canadian postal code at the END of the final line/part ('VANCOUVER BC V5Z-1V5').
# Tail-anchored so a US unit that happens to look like one ('UNIT A1B 2C3,
# SEATTLE, WA 98101') never disables the US split.
_CA_POSTAL_TAIL_RE = re.compile(r"\b[A-Z]\d[A-Z][\s-]?\d[A-Z]\d$", re.IGNORECASE)
# County data uses literal UNKNOWN placeholders ('UNKNOWN UNKNOWN, UNKNOWN WA').
_PLACEHOLDER_RE = re.compile(r"\bUNKNOWN\b", re.IGNORECASE)


def is_foreign_address(addr: str | None) -> bool:
    """True when a trailing country (or a Canadian postal code) marks a NON-US address.

    PUBLIC because skip_trace needs the same verdict: a foreign address split by US
    rules yields a fabricated state ('CANADA' -> 'CA' = California), and that state is
    what a PAID Tracerfy trace is keyed on. Case-insensitive; a single-chunk string is
    never foreign (there is no tail to read).
    """
    if not addr or not addr.strip():
        return False
    chunks = [c.strip() for c in re.split(r"[,\n]", addr.strip().rstrip(",").strip()) if c.strip()]
    if len(chunks) <= 1:
        return False
    tail_key = re.sub(r"[^A-Z ]", "", chunks[-1].upper()).strip()
    return (
        tail_key in _FOREIGN_COUNTRIES
        or tail_key.endswith(" CANADA")  # 'Toronto ON CANADA' in one part
        or bool(_CA_POSTAL_TAIL_RE.search(chunks[-1]))
    )


def strip_us_country_tail(addr: str) -> str:
    """Drop a trailing 'USA' / 'US' / 'UNITED STATES' chunk so the US tail parses normally.

    Only a WHOLE final comma/newline chunk is removed — ordinary street text that merely
    contains those letters is untouched. Returns the address stripped of a trailing comma.
    """
    clean = addr.strip().rstrip(",").strip()
    chunks = [c.strip() for c in re.split(r"[,\n]", clean) if c.strip()]
    if len(chunks) > 1:
        tail_key = re.sub(r"[^A-Z ]", "", chunks[-1].upper()).strip()
        if tail_key in _US_COUNTRY_TAILS:
            return clean[: clean.upper().rfind(chunks[-1].upper())].rstrip(" ,\n")
    return clean

# Tokens that mark a party_name as a NON-person (entity). If any appears we emit
# no first/last (the full party_name column still carries it).
_ENTITY_TOKENS = frozenset({
    "LLC", "LLP", "LP", "PLLC", "INC", "CORP", "CO", "COMPANY", "TRUST", "TR",
    "BANK", "MORTGAGE", "LOAN", "SERVICING", "ASSOCIATION", "ASSN", "HOA",
    "FUND", "HOLDINGS", "PROPERTIES", "PROPERTY", "INVESTMENTS", "INVESTMENT",
    "CAPITAL", "GROUP", "PARTNERS", "PARTNERSHIP", "ENTERPRISES", "REALTY",
    "HOMES", "CHURCH", "MINISTRIES", "AUTHORITY", "DISTRICT", "DEPARTMENT",
    "NA", "FSB", "FOUNDATION", "SERVICES", "MANAGEMENT", "VENTURES",
    # Added 2026-09-14 from a prod diff ('FOUR M ALLIANCE CORPORATION' read as a person).
    "CORPORATION", "INCORPORATED", "LIMITED", "LTD", "ASSOCIATES", "APTS", "APARTMENTS",
    "CONDOMINIUM", "CONDOMINIUMS", "OWNERS", "CLUB", "SCHOOL", "UNIVERSITY", "COLLEGE",
    "HOSPITAL", "ASSOC",
})
# 'MINADOKA L L C' / 'SAN MARCO L.L.P.' spell the entity marker out letter by letter.
_SPACED_ENTITY_RE = re.compile(r"\bL\W*L\W*[CP]\b")

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
    if any(ch.isdigit() for ch in upper) or _SPACED_ENTITY_RE.search(upper):
        return True
    # Joiners count as token breaks, or 'YESLER TOWERS LLC/CHAN J' hides its LLC.
    tokens = {re.sub(r"[^A-Z]", "", t) for t in re.split(r"[\s/\\+&;,]+", upper)}
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


# ── Source-aware first-person split (CSV export) ────────────────────────────
# split_owner_for_display above assumes recorder order for every source, which a
# prod sample (2026-09-14) proved wrong: trustee's-sale notices write the grantor in
# NATURAL order ("SHIRLEY A JOHNSON" came out first='A', last='SHIRLEY'), and a
# joined party ("JOHN AND JANE SMITH") came out first='AND'. The export now says
# which order the source uses, and an unknown order yields blanks, never a guess.
NAME_ORDER_RECORDER = "recorder"  # 'LAST FIRST [MIDDLE]'  (WA recorder / assessor)
NAME_ORDER_NATURAL = "natural"    # 'FIRST [MIDDLE] LAST'  (trustee's-sale notices)
NAME_ORDER_COMMA_ONLY = "comma_only"  # mixed source: only 'LAST, FIRST' is readable

# Two levels of joiner. PARTY separators are the explicit ones our own scrapers
# write between distinct parties (normalize_party_text joins with ' / '), so an
# entity party beside a person can be skipped safely. CO-OWNER joiners live inside
# one free-text owner cell (Snohomish 'CISSNA RICHARD C/KATHRYN A', King assessor
# 'GOSS WESLEY+MARIE', notices ' AND '); splitting on them BEFORE the entity check
# cut entities apart ('WSDOT R/E SERVICES' -> first 'R', a prod diff 2026-09-14), so
# an entity anywhere in the cell now blanks the whole cell.
_PARTY_SEP_RE = re.compile(r"\s+/\s+|\s*;\s*")
# An unspaced '&' joins only word-length names ('SMITH&JANE'), never 'AT&T' / 'B&B'.
# Snohomish also joins co-owners with a backslash ('BRIGGS JESSE T\JESSICA RAE').
_COOWNER_JOIN_RE = re.compile(
    r"\s*/\s*|\s*\\\s*|\s*\+\s*|\s+&\s+|(?<=[A-Za-z]{2})&(?=[A-Za-z]{2})|\s+AND\s+",
    re.IGNORECASE,
)
# A comma followed by a vesting/capacity clause ends the name: "JOANN SIMON, AN
# UNMARRIED INDIVIDUAL", "JOHN AND JANE SMITH, HUSBAND AND WIFE". Cut BEFORE the
# joiner split so the clause's own ' AND ' can't create a fake party.
_VESTING_TAIL_RE = re.compile(
    # A/AN/AS need a following word: 'SMITH, A' is a surname plus an initial.
    r",\s*(?:(?:A|AN|AS)\s+\w|(?:HIS|HER|THEIR|HUSBAND|WIFE|MARRIED|UNMARRIED|SINGLE|EACH|"
    r"JOINT|TENANTS?|DEALING|SEPARATE|SOLE|PERSONAL|SURVIVING)\b).*$",
    re.IGNORECASE,
)
# A party carrying one of these is a role, or vesting text without its comma
# ('JOHN SMITH HUSBAND AND WIFE'), not a person we can name reliably.
_CAPACITY_RE = re.compile(
    r"\b(?:HEIRS?|DEVISEES?|TRUSTEES?|ET\s*AL|ETAL|UNKNOWN|AS|DBA|FBO|ATTN|"
    r"DEPT|PUBLIC|COUNTY|GUARDIAN|CONSERVATOR|RECEIVER|"
    r"HUSBAND|WIFE|SPOUSES?|MARRIED|UNMARRIED|INDIVIDUAL|TENANTS?)\b",
    re.IGNORECASE,
)
# 'C/O X' names who receives the mail, not an owner; drop it before any joiner split
# turns 'C/O' into a fake co-owner ('JOHN SMITH C/O JANE DOE' -> last 'C').
_CARE_OF_RE = re.compile(r"\s*\b(?:C\s*/\s*O|C\\O|CARE\s+OF)\b.*$", re.IGNORECASE)
# Trailing decedent markers on a recorder name ('JOHNSON WILLIAM EST OF', Pierce
# 'MERCER JOANNE HEIRS OF'). The named person is the decedent the record is about,
# the same person party_name already names, so the marker is stripped, not blanked.
_TRAILING_ESTATE_RE = re.compile(
    # '(+)' is Pierce ARMS's "more parties" marker ('NEWBURY CLARECE HEIRS OF(+)').
    r"\s+(?:EST(?:ATE)?(?:\s+OF)?|HEIRS\s+OF|DECEASED|DEC'?D)\.?\s*(?:\(\+\))?\s*$",
    re.IGNORECASE,
)
# 'V' is deliberately absent: on these records it is a middle initial far more often
# than a generational suffix ('KATE V CHEFER').
_NAME_SUFFIXES = frozenset({"JR", "SR", "II", "III", "IV"})

# ── Rules added 2026-09-15 from a read-only scan of every 4+-word prod party name ──
# A trailing ROLE names a real person acting for the owner ('ALDRIDGE FAYE MARIE
# TTEE', 'ENGLER DAVID M EXEC', 'GRAY JUDSON PER REP'). The role is stripped and the
# person kept (Codex): the name itself is not corrupted by the role.
# The role must be its own word ('CHINN HING W -TTEE'); one glued to the name
# ('RITA HSIU-HUI KAO-TRUSTEE') is left alone, so it still blanks as a capacity.
_ROLE_TAIL_RE = re.compile(
    r"\s+[-(]?(?:AS\s+)?(?:TTEE|TTE|TRUSTEE|TRUSTE|EXEC|EXECUTOR|EXECUTRIX|ADMN|ADMIN|"
    r"ADMINISTRATOR|ADMINISTRATRIX|PERS?\s+REP|ESQ)\)?\.?\s*(?:\(\+\))?\s*$",
    re.IGNORECASE,
)
# An alias clause ('MENDOZA JOSE M GUILLEN AKA ...') is not part of the primary name.
_ALIAS_TAIL_RE = re.compile(r"\s+(?:AKA|FKA|NKA)\b.*$", re.IGNORECASE)
# 'LANGE CARL R MRS' is Mrs. Carl Lange: the given name is not this person's, so blank.
_HONORIFIC_RE = re.compile(r"\bMRS\b", re.IGNORECASE)

# Words no person name carries, from organization cells the entity tokens missed
# ('ISLAMIC CENTER OF KENT', 'HEIDEH EFTEHARI LIVING TRUS'). Deliberately WITHOUT
# words that are also real surnames (PARK, PARKS, TEMPLE, BIBLE, MEADOWS, HOME, REAL):
# those only count inside a phrase such as REAL ESTATE (Codex).
_ORG_WORDS = frozenset({
    "CENTER", "CENTRE", "CTR", "CITY", "STATE", "STATES", "HOUSING", "HOMEOWNERS",
    "HOMEOWNER", "ESTATES", "FAMILY", "REVOCABLE", "REVOCABL", "IRREVOCABLE", "LIVING",
    "TESTAMENTARY", "TESTMENTARY", "DECEDENTS", "BAPTIST", "BAPT", "CATHOLIC", "CATH",
    "LUTHERAN", "METHODIST", "PRESBYTERIAN", "EPISCOPAL", "MISSIONARY", "PENTECOSTAL",
    "GOSPEL", "ISLAMIC", "BUDDHIST", "SPIRITUAL", "CONGREGATION", "COMMUNITY",
    "COMMUNITIES", "DEVELOPMENT", "DEVELOPMENTS", "DEVELOPEMENT", "INVESTORS", "INVEST",
    "BUILDERS", "BUILD", "CONSTRUCTION", "CHAMBER", "COMMERCE", "MEDICAL", "TOWNHOMES",
    "APARTMENT", "TRANSIT", "AGENCY", "JOINT", "VENTURE", "DST", "LLLP", "SVC", "SVCS",
    "SHOPPING", "METROPOLITAN", "REGIONAL", "EDUCATION", "RETREAT", "NEIGHBORS",
    "REGENCY", "MARINAS", "COORDINATING", "CULTURAL", "SPORTS", "ACQUIS", "WSDOT", "DNR",
    "USA", "AMERICA", "VACANT", "PHASE", "VILLAGE", "TRAILS", "MOBILE", "ALLIANCE",
    "RESERVE", "WATER", "ORG", "WOMENS", "THOUSAND",
})
# A cut-off final word of an assessor cell ('...HOLDIN', '...ASSOCIAT') is an
# organization word only at the cell's truncation length and only at 5+ letters.
_TRUNCATED_CELL_LEN = 26
# OF/FOR anywhere; THE only as the FIRST word ('THE MEADOWS AT ROCK CREEK'), because
# 'THE' is also a Vietnamese given name ('PHAM ANH THE').
_FUNCTION_WORDS = frozenset({"OF", "FOR"})

# Common US Hispanic surnames, EXCLUDING ones that are also common given names
# (CRUZ, SANTIAGO, ROSARIO, LUNA, LARA, MIRANDA, LEON, SANTOS, PAZ, BAUTISTA ...), so
# membership is evidence a word is NOT the first name. Used only to recognise a
# SECOND surname ('ALATORRE HERNANDEZ JOSE LUIS', 'Jessica M. Hernandez Olvera').
_HISPANIC_SURNAMES = frozenset({
    "GARCIA", "RODRIGUEZ", "MARTINEZ", "HERNANDEZ", "LOPEZ", "GONZALEZ", "PEREZ",
    "SANCHEZ", "RAMIREZ", "TORRES", "FLORES", "RIVERA", "GOMEZ", "DIAZ", "REYES",
    "MORALES", "ORTIZ", "GUTIERREZ", "CHAVEZ", "RAMOS", "RUIZ", "ALVAREZ", "MENDOZA",
    "VASQUEZ", "VAZQUEZ", "CASTILLO", "JIMENEZ", "MORENO", "ROMERO", "HERRERA", "MEDINA",
    "AGUILAR", "GARZA", "CASTRO", "VARGAS", "FERNANDEZ", "GUZMAN", "MUNOZ", "MENDEZ",
    "SALAZAR", "SOTO", "DELGADO", "PENA", "RIOS", "ALVARADO", "SANDOVAL", "CONTRERAS",
    "VALDEZ", "GUERRERO", "ORTEGA", "ESTRADA", "NUNEZ", "MALDONADO", "VEGA", "DOMINGUEZ",
    "ESPINOZA", "ESPINOSA", "SILVA", "PADILLA", "MARQUEZ", "CORTEZ", "CORTES", "ROJAS",
    "ACOSTA", "FIGUEROA", "JUAREZ", "NAVARRO", "CAMPOS", "MOLINA", "AVILA", "AYALA",
    "MEJIA", "CARRILLO", "DURAN", "CABALLERO", "ROBLES", "SOLIS", "PACHECO", "SERRANO",
    "VELASQUEZ", "VELAZQUEZ", "FUENTES", "CABRERA", "CERVANTES", "ROSALES", "IBARRA",
    "VILLARREAL", "MONTOYA", "CALDERON", "ZAMORA", "TREVINO", "GALVAN", "CAMACHO",
    "BARRERA", "OLVERA", "MACIAS", "RANGEL", "SOSA", "ZUNIGA", "ARELLANO", "CARDENAS",
    "OCHOA", "BELTRAN", "QUINTERO", "OROZCO", "SALINAS", "ESPARZA", "MORA", "LARIOS",
    "GODINEZ", "GARIBAY", "MONDRAGON", "ESCOBAR", "ORELLANA", "OLIVA", "BAEZ", "GUILLEN",
    "VILLALOBOS", "VILLANUEVA", "ARIAS", "LEAL", "CORONA", "GALLARDO", "MONTES", "SALAS",
    "AGUIRRE", "LOZANO", "BARRAGAN", "BECERRA", "BRAVO", "CISNEROS", "ENRIQUEZ", "GAMEZ",
    "LUCERO", "MACHADO", "MERCADO", "NARANJO", "PALACIOS", "PANTOJA", "QUIROGA", "QUIROZ",
    "RENTERIA", "SAUCEDO", "TAPIA", "URIBE", "VALADEZ", "VALENZUELA", "VERDUGO",
    "ZAVALA", "ALATORRE", "AMBROCIO", "PELAYO", "MONTEMAYOR", "SABALZA",
})
# Vietnamese surnames that are rarely given names. One sitting where a recorder/
# assessor cell puts the GIVEN name ('VU NGUYEN SONG KHANH', 'BICH BUI THI NGOC')
# means that cell's word order is untrustworthy, so the split is blanked (Codex).
# Left out on purpose (prod diff 2026-09-15): DANG/DUONG/HOANG are common given
# names ('PHAM DANG'), and DO/HO/LY are Korean and Chinese given syllables.
_VIETNAMESE_SURNAMES = frozenset({
    "NGUYEN", "TRAN", "LE", "PHAM", "HUYNH", "PHAN", "BUI", "NGO", "VO", "VU",
})
# Export-only surname particles. Two-word sequences (DE LOS, VAN DER) bind only as a
# pair: LOS/LAS/DER/DEN alone are ordinary words (Codex). LE is absent on purpose; a
# recorder cell starting with LE is French ('LE BAUGH CHRISTOPHER MAX') or Vietnamese
# ('LE HOAI NU MINH') and the two cannot be told apart.
_EXPORT_PARTICLES = frozenset({
    "VAN", "VON", "DE", "DEL", "DELA", "LA", "DI", "DA", "DU", "DOS", "MC", "MAC", "O",
    "ST", "SAINT", "SANTA", "SAN", "AL", "EL",
})
_PARTICLE_PAIRS = frozenset({("DE", "LOS"), ("DE", "LAS"), ("VAN", "DER"), ("VAN", "DEN")})


def _looks_like_organization(party: str, cell_len: int) -> bool:
    """Organization evidence the entity tokens miss: an org word, a REAL ESTATE
    phrase, a function word (OF/THE/FOR never appear in a person's name once estate
    and heirs markers are stripped), or a truncated org word ending the cell."""
    upper = _ESTATE_PREFIX_RE.sub("", party.upper())
    words = [re.sub(r"[^A-Z]", "", w) for w in re.split(r"[\s/+&;,\-()]+", upper)]
    words = [w for w in words if w]
    if not words:
        return False
    if (
        set(words) & (_ORG_WORDS | _FUNCTION_WORDS)
        or words[0] == "THE"
        or re.search(r"\bREAL\s+ESTATE\b", upper)
    ):
        return True
    last = words[-1]
    return cell_len >= _TRUNCATED_CELL_LEN and len(last) >= 5 and any(
        w != last and w.startswith(last) for w in (_ORG_WORDS | _ENTITY_TOKENS)
    )


def _recorder_first_last(toks: list[str]) -> tuple[str | None, str | None]:
    """'LAST [LAST2] FIRST [MIDDLE]' for a recorder/assessor cell, or blanks."""
    up = [t.upper() for t in toks]
    if len(toks) >= 3 and len(toks[1]) == 1 and len(toks[-1]) > 1:
        # 'LAVENDER A LORENE' (LAST F MIDDLE) and a natural-order name that leaked
        # into a recorder field ('STEPHEN P MYERS') have the SAME shape; no rule
        # tells them apart, so neither half is exported (prod diff 2026-09-14).
        return None, None
    if len(toks) >= 3 and up[0] == "LE":
        return None, None
    end = 0  # index of the last surname word
    if len(toks) >= 3:
        while end < len(toks) - 2:
            if (up[end], up[end + 1]) in _PARTICLE_PAIRS:
                end += 2
            elif up[end] in _EXPORT_PARTICLES:
                end += 1
            else:
                break
    nxt = end + 1
    if nxt + 1 < len(toks) and up[nxt] in _HISPANIC_SURNAMES:
        if up[nxt + 1] in _HISPANIC_SURNAMES:
            return None, None  # three surname-looking words: no reliable boundary
        end = nxt  # second surname ('GUZMAN CAMPOS MARIA F')
    if end + 1 >= len(toks):
        return None, None
    if up[end + 1] in _VIETNAMESE_SURNAMES:
        return None, None
    first, last = toks[end + 1], " ".join(toks[: end + 1])
    if " " in last and not _drop_initial(first):
        # A multi-word surname followed only by an initial is a misread ('LE MAI H').
        return None, None
    return _drop_initial(first), last


def _first_person_tokens(cand: str, name_order: str) -> tuple[str | None, str | None]:
    """(first, last) of ONE candidate party in the declared order, or blanks."""
    if "," in cand:
        # 'LAST, FIRST [MIDDLE]' is order-unambiguous, EXCEPT in a natural-order
        # notice, where a comma is just as often a list separator
        # ("INGABIRE UQIMANA, JUDITH UMUTONI AND ...") — blank there.
        if name_order == NAME_ORDER_NATURAL or cand.count(",") > 1:
            return None, None
        last_part, _, rest = cand.partition(",")
        last_toks = [t for t in _NAME_TOKEN_RE.findall(last_part) if t.upper() not in _NAME_SUFFIXES]
        first_toks = [t for t in _NAME_TOKEN_RE.findall(rest) if t.upper() not in _NAME_SUFFIXES]
        if not last_toks or not first_toks:
            return None, None
        return _drop_initial(first_toks[0]), " ".join(last_toks)
    if name_order == NAME_ORDER_COMMA_ONLY:
        return None, None
    toks = [t for t in _NAME_TOKEN_RE.findall(cand) if t.upper() not in _NAME_SUFFIXES]
    if len(toks) < 2:
        return None, None  # a lone token ('JOHN' of 'JOHN AND JANE SMITH') is not a name
    if name_order == NAME_ORDER_RECORDER:
        return _recorder_first_last(toks)
    # Natural order: last token is the surname, with any particles bound to it
    # ('MARY VAN DYKE' -> VAN DYKE).
    up = [t.upper() for t in toks]
    j = len(toks) - 1
    if len(toks) >= 4 and up[-2] == "Y" and up[-3] in _HISPANIC_SURNAMES:
        j = len(toks) - 3  # 'MARIA GARCIA Y LOPEZ'
    elif len(toks) >= 3 and up[-2] in _HISPANIC_SURNAMES and up[-1] in _HISPANIC_SURNAMES:
        # Both final words are surnames ('Jessica M. Hernandez Olvera'); a single
        # list word before a non-list surname stays a middle name.
        j = len(toks) - 2
    while j > 1 and up[j - 1] in _SURNAME_PARTICLES:
        j -= 1
    return _drop_initial(toks[0]), " ".join(toks[j:])


def _drop_initial(first: str | None) -> str | None:
    """A bare initial ('A' of 'A JOHNSON', 'J' of 'CHAN J') is not a first name. The
    surname still reads confidently, so only First goes blank."""
    return first if first and len(first) > 1 else None


def split_first_person(
    party_name: str | None, name_order: str | None
) -> tuple[str | None, str | None]:
    """(first_name, last_name) of the FIRST-LISTED individual in ``party_name``.

    Export semantics (documented contract): First/Last name one person, the first
    co-owner of the first non-entity party on the record, when that person's full
    name reads unambiguously in the source's declared ``name_order``. An entity
    PARTY (a bank listed beside the owner with ' / ') is skipped; an entity anywhere
    inside one owner cell, a role (HEIRS, TRUSTEE, AS ..., ET AL), or a lone given
    name ('JOHN' of 'JOHN AND JANE SMITH') yields blanks. ``name_order`` None
    (source order unknown) always yields blanks. The full ``party_name`` column is
    always exported alongside, so nothing is lost.
    """
    if not party_name or not party_name.strip() or name_order is None:
        return None, None
    text_ = _VESTING_TAIL_RE.sub("", party_name.strip())
    for raw in _PARTY_SEP_RE.split(text_):
        owner_cell = _ALIAS_TAIL_RE.sub("", _CARE_OF_RE.sub("", raw)).strip()
        party = _TRAILING_ESTATE_RE.sub("", owner_cell).strip()
        party = _TRAILING_ESTATE_RE.sub("", _ROLE_TAIL_RE.sub("", party)).strip()
        if not party:
            continue
        if _is_entity(party) or _looks_like_organization(party, len(raw.strip())):
            continue
        if _CAPACITY_RE.search(party) or _HONORIFIC_RE.search(party):
            return None, None
        cand = _COOWNER_JOIN_RE.split(party)[0].strip()
        order = name_order
        if _ESTATE_PREFIX_RE.match(cand):
            cand = _ESTATE_PREFIX_RE.sub("", cand).strip()
            # A mixed source writes 'ESTATE OF GLENNA K JONES' in natural order. A
            # recorder/assessor source writes BOTH orders after the prefix (prod:
            # 'ESTATE OF KLUG DORIS ANN' and 'ESTATE OF RICHARD TODD'), so only its
            # order-free comma form ('ESTATE OF SMITH, JOHN') is read there.
            if name_order == NAME_ORDER_COMMA_ONLY:
                order = NAME_ORDER_NATURAL
            elif name_order == NAME_ORDER_RECORDER:
                order = NAME_ORDER_COMMA_ONLY
        cand = _TRAILING_ESTATE_RE.sub("", cand).strip()
        return _first_person_tokens(cand, order)
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

    # Placeholder-only text ('UNKNOWN UNKNOWN, UNKNOWN WA') is no address at all.
    if _PLACEHOLDER_RE.search(clean) and not re.sub(
        r"[\s,]|\b[A-Za-z]{2}\b", "", _PLACEHOLDER_RE.sub("", clean)
    ):
        return out
    if is_foreign_address(clean):
        out["street"] = clean
        return out
    # '..., WA 98101, USA' — drop the country so the US tail parses normally.
    clean = strip_us_country_tail(clean)

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
        if (
            not _UNIT_RE.match(cand)
            and not any(ch.isdigit() for ch in cand)
            and not _PLACEHOLDER_RE.search(cand)
        ):
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
