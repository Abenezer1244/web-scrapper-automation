"""Asotin County parcels owned by a probate decedent, from the county's own parcel layer.

Asotin's PACS portal offers no owner-name search (only account number, address and DBA);
we do not work around that. The county publishes its assessor parcels as a public ArcGIS
layer (linked from asotincountywa.gov/179/Parcel-Map, "search for parcels by owner") with
the owner NAME, billing (mailing) lines and situs address, and that layer is what this
module reads.

THE MATCH RULE (owner decision 2026-11-14, "name + family tie"). A notice names a person,
never a property, and a name alone could be a living namesake (Codex). A parcel is tied to
the estate only when BOTH hold:
  1. EXACT NAME: the parcel's owner (any co-owner) has the decedent's last name, first name
     and suffix, and, when the notice gives a middle name or initial, a compatible middle.
     Never fuzzy, never a prefix.
  2. A FAMILY TIE, any of: the owner record is marked as an estate; a personal
     representative is named on the parcel (owner or billing lines); a personal
     representative shares the decedent's surname; the billing address is the notice's
     address for claims.
If the decedent's exactly-matched parcels are billed to different mailing addresses, they
may be two people, so the notice is held. A tied parcel without a usable property or
mailing address is held too. Everything held is reported with its reason, never delivered.
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
_PAGE = 1000  # the layer's maxRecordCount is 2000; a surname never comes close

_SUFFIXES = frozenset({"JR", "SR", "II", "III", "IV"})
# Words that end the person part of an owner segment ("SMITH, JOHN A ESTATE C/O ...").
_NOISE = frozenset({"ESTATE", "EST", "TRST", "TRUST", "TRUSTEE", "TRUSTEES", "TR", "ETAL",
                    "ET", "AL", "C/O", "PR", "LIVING", "FAMILY", "REVOCABLE", "DECEASED",
                    "DECD", "DEC'D", "HEIRS", "OF"})
_CORPORATE = re.compile(r"\b(?:LLC|INC|CORP|COMPANY|CO|LLP|LP|BANK|CHURCH|CITY|COUNTY|STATE"
                        r"|DISTRICT|ASSOCIATION|ASSN|PROPERTIES|INVESTMENTS?)\b")
# A decedent's estate, not a life estate, real estate or a subdivision ("ESTATES").
_ESTATE_MARK = re.compile(
    r"(?<!LIFE )(?<!REAL )\bESTATE\b(?!S)|\(EST\)|\bEST OF\b|\bDECEASED\b|\bDEC'?D\b")
_STREET_LINE = re.compile(r"^(?:\d|P\.?\s*O\.?\s*BOX\b|POST OFFICE BOX\b|BOX\b|PMB\b|RR\b|HC\b)")
_SAFE_SURNAME = re.compile(r"^[A-Z][A-Z'\-]{1,40}$")


class LayerError(RuntimeError):
    """The layer did not answer (HTTP error, ArcGIS error body, truncated page). Never a
    "no parcels" answer."""


@dataclass(frozen=True)
class Person:
    first: str
    middle: str
    last: str
    suffix: str


@dataclass(frozen=True)
class ParcelLead:
    parcel_id: str
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
    return [t for t in re.sub(r"[^A-Z0-9'\- ]", " ", (text or "").upper()).split() if t]


def notice_person(name: str) -> Person | None:
    """'COLLEEN J. OZARD' / 'JOHN A. DOE, JR.' -> Person; None if it has no first + last."""
    toks = _tokens(name)
    suffix = ""
    if toks and toks[-1] in _SUFFIXES:
        suffix = toks.pop()
    if len(toks) < 2:
        return None
    return Person(toks[0], " ".join(toks[1:-1]), toks[-1], suffix)


def owner_people(owner_name: str) -> list[Person]:
    """The people named in a layer NAME ('LAST, FIRST M & FIRST2', 'LAST JR, FIRST',
    'LAST, FIRST & LAST2, FIRST2', 'LAST, JR, FIRST'). Organizations yield nobody."""
    upper = (owner_name or "").upper()
    if _CORPORATE.search(upper):
        return []
    people: list[Person] = []
    last = ""
    for seg in upper.split("&"):
        parts = [p.strip() for p in seg.split(",") if p.strip()]
        suffix = ""
        if len(parts) >= 2:
            lt = _tokens(parts[0])
            if lt and lt[-1] in _SUFFIXES:
                suffix = lt.pop()
            if parts[1].strip(" .") in _SUFFIXES:   # 'HERNANDEZ, JR, NAT'
                suffix, parts = parts[1].strip(" ."), [parts[0]] + parts[2:]
            last = " ".join(lt)
            given_src = " ".join(parts[1:])
        elif last:                                   # '& MYRANDA S' continues the surname
            given_src = parts[0] if parts else ""
        else:
            continue
        given: list[str] = []
        for tok in _tokens(given_src):
            if tok in _NOISE or tok.startswith("("):
                break
            given.append(tok)
        if given and given[-1] in _SUFFIXES:
            suffix = given.pop()
        if last and given:
            people.append(Person(given[0], " ".join(given[1:]), last, suffix))
    return people


def _middle_ok(notice_middle: str, owner_middle: str) -> bool:
    if not notice_middle:
        return True          # the notice gave no middle: any owner middle is the same name
    if not owner_middle:
        return False         # the notice gave one and the record has none: not exact
    a, b = notice_middle.split()[0], owner_middle.split()[0]
    if len(a) == 1 or len(b) == 1:
        return a[0] == b[0]
    return a == b


def same_person(notice: Person, owner: Person) -> bool:
    return (notice.last == owner.last and notice.first == owner.first
            and notice.suffix == owner.suffix and _middle_ok(notice.middle, owner.middle))


def _street_key(text: str | None) -> tuple[str, str] | None:
    """(house/box number, zip) of an address, for comparing two of them."""
    up = (text or "").upper()
    num = re.search(r"\b(?:BOX|DRAWER)\s*(\d+)|^\s*(\d+)\b|\b(\d+)\s+[A-Z]", up)
    zipc = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", up)
    if not (num and zipc):
        return None
    return (next(g for g in num.groups() if g), zipc.group(1))


def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def mailing_of(attrs: dict) -> str | None:
    """'STREET, CITY, ST ZIP' from the billing lines: the first street/box line and what
    follows it; lines before it are addressees (C/O, a co-owner) and are not an address."""
    lines = [_clean(attrs.get("BILLING1")), _clean(attrs.get("BILLING2"))]
    start = next((i for i, ln in enumerate(lines) if _STREET_LINE.match(ln.upper())), None)
    city, st, zipc = _clean(attrs.get("CITY")), _clean(attrs.get("ST")), _clean(attrs.get("ZIP"))
    if start is None or not (city and re.fullmatch(r"[A-Z]{2}", st.upper())
                             and re.fullmatch(r"\d{5}", zipc)):
        return None
    street = " ".join(ln for ln in lines[start:] if ln)
    return f"{street}, {city}, {st.upper()} {zipc}"


def _ties(attrs: dict, decedent: Person, reps: list[Person], claims_address: str | None,
          mailing: str | None) -> list[str]:
    name = _clean(attrs.get("NAME")).upper()
    billing = " ".join(_clean(attrs.get(k)).upper() for k in ("BILLING1", "BILLING2"))
    out = []
    if _ESTATE_MARK.search(name):
        out.append("estate_marked")
    words = set(_tokens(f"{name} {billing}"))
    if any(r.first in words and r.last in words for r in reps):
        out.append("rep_on_parcel")
    if any(r.last == decedent.last for r in reps):
        out.append("rep_shares_surname")
    claims = _street_key(claims_address)
    if claims is not None and claims == _street_key(mailing):
        out.append("billing_is_claims_address")
    return out


def match_estate(decedent_name: str, personal_reps: tuple[str, ...] | list[str],
                 claims_address: str | None, rows: list[dict]) -> EstateMatch:
    """Decide which of ``rows`` (layer attributes) are the estate's parcels. Pure."""
    result = EstateMatch()
    decedent = notice_person(decedent_name)
    if decedent is None:
        result.held.append("decedent_name_unreadable")
        return result
    reps = [p for p in (notice_person(r) for r in personal_reps) if p is not None]
    matched = [a for a in rows
               if any(same_person(decedent, o) for o in owner_people(_clean(a.get("NAME"))))]
    result.name_matched = len(matched)
    if not matched:
        result.held.append("no_parcel_in_decedents_name")
        return result
    mailings = {mailing_of(a) for a in matched}
    if len({m for m in mailings if m}) > 1:
        result.held.append("matched_parcels_billed_to_different_addresses")
        return result
    for a in matched:
        mailing = mailing_of(a)
        ties = _ties(a, decedent, reps, claims_address, mailing)
        if not ties:
            result.held.append("no_family_tie")
            continue
        situs = _clean(a.get("FULL_ADD"))
        parcel = _clean(a.get("PARCEL_ID"))
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
            parcel_id=parcel, prop_id=_clean(a.get("Prop_ID")),
            owner_name=_clean(a.get("NAME")), property_address=situs,
            property_city=_clean(a.get("SITE_CITY")) or None,
            property_zip=_clean(a.get("SITE_ZIP")) or None,
            mailing_address=mailing, ties=tuple(ties)))
    return result


def query_surname(surname: str) -> list[dict]:
    """Every parcel whose owner NAME contains ``surname`` (filtered exactly by the caller).

    Raises LayerError when the layer gives no trustworthy answer, and ValueError for a
    surname that is not a plain name (it is quoted into the where clause)."""
    surname = surname.upper()
    if not _SAFE_SURNAME.match(surname):
        raise ValueError("surname is not a plain name")
    params = {
        "where": "NAME LIKE '%" + surname.replace("'", "''") + "%'",
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
    if not isinstance(data, dict) or data.get("error") or "features" not in data:
        raise LayerError("layer answered an error body")
    if data.get("exceededTransferLimit"):
        raise LayerError("layer truncated the answer")
    return [f.get("attributes") or {} for f in data["features"]]
