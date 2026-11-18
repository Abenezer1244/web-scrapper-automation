"""Asotin County parcels owned by a probate decedent, from the county's own parcel layer.

Asotin's PACS portal offers no owner-name search (only account number, address and DBA);
we do not work around that. The county publishes its assessor parcels as a public ArcGIS
layer (linked from asotincountywa.gov/179/Parcel-Map, "search for parcels by owner") with
the owner NAME, billing (mailing) lines and situs address, and that layer is what this
module reads.

THE MATCH RULE (owner decision 2026-11-14, "name + family tie"). A notice names a person,
never a property, and a name alone could be a living namesake (Codex). A parcel is tied to
the estate only when BOTH hold:
  1. EXACT NAME: one owner named on the parcel has the decedent's surname, first name and
     suffix, and every middle name or initial the notice gives (an initial agrees with a
     full name). Never fuzzy, never a prefix.
  2. A FAMILY TIE, any of: THAT owner's part of the record is marked as an estate; a
     personal representative is a person named on the parcel (an owner or a C/O or
     billing addressee, compared as a whole name); a representative shares the
     decedent's surname; the billing address is the notice's address for claims (same
     street line and ZIP, compared in full).
If the decedent's exactly-matched parcels are billed to different mailing addresses, they
may be two people, so the notice is held. A tied parcel without a usable property or
mailing address is held too. An owner record whose layout is ambiguous names nobody.
Everything held is reported with its reason, never delivered.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get

_logger = setup_logger("scraper.enrichment.asotin_parcels")

LAYER_URL = ("https://services6.arcgis.com/VsJGTl1jmCYBc0EX/arcgis/rest/services"
             "/Parcels/FeatureServer/0/query")
OUT_FIELDS = ("PARCEL_ID", "Prop_ID", "NAME", "BILLING1", "BILLING2", "CITY", "ST", "ZIP",
              "FULL_ADD", "SITE_CITY", "SITE_ZIP")
# The layer's maxRecordCount is 2000. A surname rarely comes close; a short letter run
# (surname_where) can, and then the answer is truncated and the lookup fails (LayerError).
_PAGE = 1000

_SUFFIXES = frozenset({"JR", "SR", "II", "III", "IV"})
# Words that end the person part of an owner segment ("SMITH, JOHN A ESTATE C/O ...").
_NOISE = frozenset({"ESTATE", "EST", "TRST", "TRUST", "TRUSTEE", "TRUSTEES", "TR", "ETAL",
                    "ET", "AL", "PR", "LIVING", "FAMILY", "REVOCABLE",
                    "DECEASED", "DECD", "DEC'D", "HEIRS", "OF", "LIFE"})
_CORPORATE = re.compile(r"\b(?:LLC|INC|CORP|COMPANY|LLP|LP|BANK|CHURCH|CITY|COUNTY|STATE"
                        r"|DISTRICT|ASSOCIATION|ASSN|PROPERTIES|INVESTMENTS?|BUSINESS)\b")
# A decedent's estate; never a life estate, real estate or a subdivision ("ESTATES").
# Read on whitespace/punctuation-normalized text, so "LIFE-ESTATE" is "LIFE ESTATE".
_ESTATE_MARK = re.compile(r"\bESTATE\b|\bEST\b(?= OF\b|$)|\bDECEASED\b|\bDEC'?D\b")
_NOT_DECEDENT_ESTATE = re.compile(r"\b(?:LIFE|REAL)\s+ESTATE\b|\bESTATES\b")
_STREET_LINE = re.compile(r"^(?:\d|P\.?\s*O\.?\s*(?:BOX|DRAWER)\b|POST OFFICE BOX\b|BOX\b"
                          r"|PMB\b|RR\b|HC\b)")
# An addressee line naming a trust or an estate is an entity, not the person in it.
_ORG_WORDS = frozenset({"TRUST", "TRST", "TRUSTEE", "TRUSTEES", "ESTATE", "FAMILY", "LIVING",
                        "REVOCABLE", "IRREVOCABLE", "PARTNERSHIP", "HEIRS"})
_TEXT_FIELDS = ("PARCEL_ID", "NAME", "BILLING1", "BILLING2", "CITY", "ST", "ZIP", "FULL_ADD",
                "SITE_CITY", "SITE_ZIP")
_SAFE_SURNAME = re.compile(r"[A-Z][A-Z'\-]{1,40}")       # fullmatch only
_LETTER_RUN = re.compile(r"[A-Z]+")
_SAFE_TOKEN = re.compile(r"[A-Z]{2,41}")                  # fullmatch only
_ADDR_WORDS = {
    "STREET": "ST", "AVENUE": "AVE", "AV": "AVE", "DRIVE": "DR", "ROAD": "RD",
    "BOULEVARD": "BLVD", "COURT": "CT", "LANE": "LN", "PLACE": "PL", "CIRCLE": "CIR",
    "HIGHWAY": "HWY", "PARKWAY": "PKWY", "TERRACE": "TER", "NORTH": "N", "SOUTH": "S",
    "EAST": "E", "WEST": "W", "APARTMENT": "APT", "SUITE": "STE", "UNIT": "UNIT",
}


class LayerError(RuntimeError):
    """The layer did not answer (HTTP error, ArcGIS error body, truncated or malformed
    page). Never a "no parcels" answer."""


@dataclass(frozen=True)
class Person:
    first: str
    middle: str
    last: str
    suffix: str


@dataclass(frozen=True)
class ParcelLead:
    parcel_id: str
    party_name: str        # the decedent as the county holds them: 'LAST, FIRST M SUFFIX'
    prop_id: str
    owner_name: str
    property_address: str
    property_city: str | None
    property_zip: str | None
    mailing_address: str
    ties: tuple[str, ...]


@dataclass
class EstateMatch:
    leads: list[ParcelLead] = field(default_factory=list)
    held: list[str] = field(default_factory=list)  # reasons, in order seen
    name_matched: int = 0


def _tokens(text: str) -> list[str]:
    return re.sub(r"[^A-Z0-9' ]", " ", (text or "").upper()).split()


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^A-Z0-9']", " ", (text or "").upper()).split())


@dataclass(frozen=True)
class NoticeName:
    """A name as a notice prints it ('MARGARET J. HOLLOWAY'): natural order, surname last."""
    tokens: tuple[str, ...]
    suffix: str

    @property
    def last(self) -> str:
        return self.tokens[-1]


def notice_name(name: str) -> NoticeName | None:
    """'MARGARET J. HOLLOWAY' / 'JOHN A. DOE, JR.'; None without a first name and a surname."""
    toks = _tokens(name)
    suffix = ""
    if toks and toks[-1] in _SUFFIXES:
        suffix = toks.pop()
    return NoticeName(tuple(toks), suffix) if len(toks) >= 2 else None


def notice_person(name: str) -> Person | None:
    n = notice_name(name)
    return None if n is None else Person(n.tokens[0], " ".join(n.tokens[1:-1]), n.last, n.suffix)


def _given_names(src: str) -> tuple[list[str], str]:
    """Given names before the first noise word, and a trailing suffix."""
    given: list[str] = []
    for tok in _tokens(src):
        if tok in _NOISE or tok.startswith("("):
            break
        given.append(tok)
    suffix = ""
    if given and given[-1] in _SUFFIXES:
        suffix = given.pop()
    return given, suffix


def owner_segments(owner_name: str) -> list[tuple[Person, str]]:
    """Each person named in a layer NAME with the raw text of their own segment:
    'LAST, FIRST M & FIRST2', 'LAST JR, FIRST', 'LAST, FIRST & LAST2, FIRST2',
    'LAST, JR, FIRST'. A segment that is an organization, or whose layout is ambiguous
    ('& JANE SMITH': a middle name or another surname?), names nobody."""
    out: list[tuple[Person, str]] = []
    last = ""
    for seg in (owner_name or "").upper().split("&"):
        seg = seg.split("C/O", 1)[0]          # an addressee, not an owner
        if _CORPORATE.search(seg):
            last = ""
            continue
        parts = [p.strip() for p in seg.split(",") if p.strip()]
        if len(parts) >= 2:
            lt = _tokens(parts[0])
            suffix = ""
            if lt and lt[-1] in _SUFFIXES:
                suffix = lt.pop()
            rest = parts[1:]
            if rest and rest[0].strip(" .") in _SUFFIXES:     # 'HERNANDEZ, JR, NAT'
                suffix, rest = rest[0].strip(" ."), rest[1:]
            given, gsuffix = _given_names(" ".join(rest))
            suffix = gsuffix or suffix
            last = " ".join(lt)
        elif last and parts:
            given, suffix = _given_names(parts[0])
            # Inherit the surname only for a bare given name and initials ('MYRANDA S').
            if not given or any(len(t) > 1 for t in given[1:]):
                continue
        else:
            continue
        if last and given:
            out.append((Person(given[0], " ".join(given[1:]), last, suffix), seg))
    return out


def comma_name(p: Person) -> str:
    """'LAST, FIRST MIDDLE SUFFIX': recorder order, readable without guessing."""
    return f"{p.last}, " + " ".join(x for x in (p.first, p.middle, p.suffix) if x)


def owner_people(owner_name: str) -> list[Person]:
    return [p for p, _ in owner_segments(owner_name)]


def _middles_ok(notice_middles: list[str], owner_middles: list[str]) -> bool:
    """Every middle the notice gives must agree, in order (an initial agrees with a name
    starting with it). Extra owner middles are allowed."""
    if len(owner_middles) < len(notice_middles):
        return False
    for a, b in zip(notice_middles, owner_middles, strict=False):
        if len(a) == 1 or len(b) == 1:
            if a[0] != b[0]:
                return False
        elif a != b:
            return False
    return True


def name_matches(notice: NoticeName, owner: Person) -> bool:
    """The notice's name is this owner's, exactly (multi-word surnames included)."""
    last = owner.last.split()
    k = len(last)
    if len(notice.tokens) < k + 1 or list(notice.tokens[-k:]) != last:
        return False
    return (notice.tokens[0] == owner.first and notice.suffix == owner.suffix
            and _middles_ok(list(notice.tokens[1:-k]), owner.middle.split()))


def _natural_same(rep: NoticeName, other: NoticeName) -> bool:
    """Two natural-order names are the same person: same words in order (an initial
    agrees with a name starting with it) and the same suffix. No surname is assumed."""
    if rep.suffix != other.suffix or len(rep.tokens) != len(other.tokens):
        return False
    if rep.tokens[0] != other.tokens[0] or rep.tokens[-1] != other.tokens[-1]:
        return False
    return _middles_ok(list(rep.tokens[1:-1]), list(other.tokens[1:-1]))


def _addressees(attrs: dict) -> tuple[list[Person], list[NoticeName]]:
    """People named on the parcel besides the owners: 'C/O <name>' in the NAME or the
    billing lines, and billing addressee lines. Comma-order lines are read like owners;
    natural-order ones are kept whole (their surname boundary is unknown). Street lines
    and organizations name nobody."""
    people: list[Person] = []
    natural: list[NoticeName] = []
    name = _clean(attrs.get("NAME")).upper()
    lines = [name.split("C/O", 1)[1]] if "C/O" in name else []
    for key in ("BILLING1", "BILLING2"):
        ln = _clean(attrs.get(key)).upper()
        if not ln or _STREET_LINE.match(ln):
            continue
        lines.append(ln)
    for ln in lines:
        if ln.startswith("C/O"):
            ln = ln[3:]
        if _CORPORATE.search(ln) or _ORG_WORDS & set(_tokens(ln)):
            continue
        if "," in ln:
            people += owner_people(ln)
        else:
            given, suffix = _given_names(ln)
            if len(given) >= 2:
                natural.append(NoticeName(tuple(given), suffix))
    return people, natural


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm_address(text: str) -> str:
    up = _norm(text)
    up = re.sub(r"\bP O\b|\bPOST OFFICE\b", "PO", up)
    return " ".join(_ADDR_WORDS.get(t, t) for t in up.split())


def mailing_parts(attrs: dict) -> tuple[str, str, str, str] | None:
    """(street lines, city, state, zip) from the billing lines: the first street/box line
    and what follows it; lines before it are addressees (C/O, a co-owner)."""
    lines = [_clean(attrs.get("BILLING1")), _clean(attrs.get("BILLING2"))]
    start = next((i for i, ln in enumerate(lines) if _STREET_LINE.match(ln.upper())), None)
    city, st, zipc = _clean(attrs.get("CITY")), _clean(attrs.get("ST")).upper(), _clean(attrs.get("ZIP"))
    if start is None or not (city and re.fullmatch(r"[A-Z]{2}", st) and re.fullmatch(r"\d{5}", zipc)):
        return None
    return " ".join(ln for ln in lines[start:] if ln), city, st, zipc


def mailing_of(attrs: dict) -> str | None:
    parts = mailing_parts(attrs)
    return None if parts is None else f"{parts[0]}, {parts[1]}, {parts[2]} {parts[3]}"


def _billing_is_claims(attrs: dict, claims_address: str | None) -> bool:
    """The billing street line, in full, appears in the claims address with its ZIP."""
    parts = mailing_parts(attrs)
    if parts is None or not claims_address:
        return False
    claims = _norm_address(claims_address)
    street = _norm_address(parts[0])
    if not street:
        return False
    # The billing street line in full, followed by NOTHING but the billing city, state and
    # ZIP: any other word there (a unit, a floor, a suite) makes it another address.
    tail = rf"{re.escape(_norm_address(parts[1]))} {parts[2]} {parts[3]}(?: \d{{4}})?"
    return re.search(rf"(?:^| ){re.escape(street)} {tail}$", claims) is not None


def _estate_marked(segment: str) -> bool:
    text = _norm(segment)
    return bool(_ESTATE_MARK.search(_NOT_DECEDENT_ESTATE.sub(" ", text)))


def _ties(attrs: dict, decedent_owners: list[tuple[Person, str]],
          reps: list[NoticeName], claims_address: str | None) -> list[str]:
    out = []
    if any(_estate_marked(seg) for _, seg in decedent_owners):
        out.append("estate_marked")
    people, natural = _addressees(attrs)
    people += owner_people(_clean(attrs.get("NAME")))
    if any(name_matches(r, p) for r in reps for p in people) or any(
            _natural_same(r, n) for r in reps for n in natural):
        out.append("rep_on_parcel")
    # The decedent's WHOLE surname as the record holds it ("DE LA CRUZ", not "CRUZ").
    surnames = {tuple(p.last.split()) for p, _ in decedent_owners}
    if any(len(r.tokens) > len(sn) and r.tokens[-len(sn):] == sn for r in reps for sn in surnames):
        out.append("rep_shares_surname")
    if _billing_is_claims(attrs, claims_address):
        out.append("billing_is_claims_address")
    return out


def match_estate(decedent_name: str, personal_reps: tuple[str, ...] | list[str],
                 claims_address: str | None, rows: list[dict]) -> EstateMatch:
    """Decide which of ``rows`` (layer attributes) are the estate's parcels. Pure."""
    result = EstateMatch()
    decedent = notice_name(decedent_name)
    if decedent is None:
        result.held.append("decedent_name_unreadable")
        return result
    reps = [n for n in (notice_name(r) for r in personal_reps) if n is not None]
    matched: list[tuple[dict, list[tuple[Person, str]]]] = []
    for a in rows:
        owners = [(p, seg) for p, seg in owner_segments(_clean(a.get("NAME")))
                  if name_matches(decedent, p)]
        if owners:
            matched.append((a, owners))
    result.name_matched = len(matched)
    if not matched:
        result.held.append("no_parcel_in_decedents_name")
        return result
    if len({m for m in (mailing_of(a) for a, _ in matched) if m}) > 1:
        result.held.append("matched_parcels_billed_to_different_addresses")
        return result
    for a, owners in matched:
        ties = _ties(a, owners, reps, claims_address)
        if not ties:
            result.held.append("no_family_tie")
            continue
        situs = _clean(a.get("FULL_ADD"))
        parcel = _clean(a.get("PARCEL_ID"))
        mailing = mailing_of(a)
        if not re.fullmatch(r"\d{16,17}", parcel):
            result.held.append("parcel_id_unreadable")
            continue
        if not re.match(r"\d", situs):
            result.held.append("missing_property_address")
            continue
        if not mailing:
            result.held.append("missing_mailing_address")
            continue
        result.leads.append(ParcelLead(
            parcel_id=parcel, party_name=comma_name(owners[0][0]),
            prop_id=_clean(a.get("Prop_ID")),
            owner_name=_clean(a.get("NAME")), property_address=situs,
            property_city=_clean(a.get("SITE_CITY")) or None,
            property_zip=_clean(a.get("SITE_ZIP")) or None,
            mailing_address=mailing, ties=tuple(ties)))
    return result


def parse_layer_response(data: object) -> list[dict]:
    """The attribute dicts of a layer query answer; LayerError for anything that is not
    a complete, well-formed answer (an error body, a truncated page, a malformed feature).
    An empty feature list is a real "no parcels"."""
    if not isinstance(data, dict) or data.get("error"):
        raise LayerError("layer answered an error body")
    if data.get("exceededTransferLimit"):
        raise LayerError("layer truncated the answer")
    features = data.get("features")
    if not isinstance(features, list):
        raise LayerError("layer answer has no feature list")
    rows = []
    for f in features:
        attrs = f.get("attributes") if isinstance(f, dict) else None
        if (not isinstance(attrs, dict) or not isinstance(attrs.get("NAME"), str)
                or not isinstance(attrs.get("PARCEL_ID"), str)
                or any(not isinstance(attrs.get(k), (str, type(None))) for k in _TEXT_FIELDS)
                or isinstance(attrs.get("Prop_ID"), bool)
                or not isinstance(attrs.get("Prop_ID"), (str, int, type(None)))):
            raise LayerError("layer answered a malformed feature")
        rows.append(attrs)
    return rows


def surname_where(surname: str) -> str:
    """The layer's where clause for ``surname``: NAME contains its longest letter run.

    The hosted layer takes no query parameters (its ``text`` parameter is ignored), so
    the clause is built here, from A-Z letters only: no quote, hyphen or anything else
    from the notice ever reaches it. O'BRIEN asks for BRIEN and SMITH-JONES for SMITH
    (the first of equal runs); both still return every O'BRIEN / SMITH-JONES parcel,
    and the caller keeps exact name matches only. ValueError for anything else."""
    surname = surname.upper()
    if not _SAFE_SURNAME.fullmatch(surname):
        raise ValueError("surname is not a plain name")
    token = max(_LETTER_RUN.findall(surname), key=len)   # max keeps the first of a tie
    if not _SAFE_TOKEN.fullmatch(token):
        raise ValueError("surname has no two-letter run to search for")
    return f"NAME LIKE '%{token}%'"


def query_surname(surname: str) -> list[dict]:
    """Every parcel whose owner NAME contains ``surname``'s longest letter run (filtered
    exactly by the caller).

    Raises LayerError when the layer gives no trustworthy answer, and ValueError for a
    surname that is not a plain name (surname_where)."""
    params = {
        "where": surname_where(surname),
        "outFields": ",".join(OUT_FIELDS), "returnGeometry": "false",
        "resultRecordCount": str(_PAGE), "f": "json",
    }
    try:
        resp = safe_get(LAYER_URL, params=params, timeout=20)
    except Exception as exc:  # noqa: BLE001 — network failure is a failed lookup, not zero
        raise LayerError(f"layer request failed: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise LayerError(f"layer HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise LayerError("layer answered non-JSON") from exc
    return parse_layer_response(data)
