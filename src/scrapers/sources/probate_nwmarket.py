"""Asotin County probate notices to creditors, from the Lewiston Tribune (nwmarket.com).

RCW 11.40.020 requires a personal representative to publish a notice to creditors in a
legal newspaper of the county. Asotin's appear in the Lewiston Tribune's legal notices,
the same nwmarket.com feed ``nts_nwmarket`` already crawls for trustee sales (one notice
per PDF). The feed also carries Spokane, Nez Perce and Latah probates, so a notice is
Asotin's only when BOTH its cause number carries Asotin's Superior Court county code
(``NN-4-NNNNN-02``: case type 4 is probate, 02 is Asotin) AND it names the Asotin court.

Input is the crawler's normalized PDF text (one line, hyphenation joined). Pure functions,
no network.

A notice names the decedent, the personal representative(s) and an address for claims.
It never names a property: ``enrichment.asotin_parcels`` decides whether a parcel can be
tied to the estate (owner decision 2026-11-14).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

SOURCE = "lmtribune_probate"
COUNTY = "asotin"

_ASOTIN_CAUSE = re.compile(r"\b(\d{2}-4-\d{5}-02)\b")
_ASOTIN_COURT = re.compile(
    r"SUPERIOR\s+COURT[^.]{0,60}?(?:FOR\s+(?:THE\s+)?COUNTY\s+OF\s+ASOTIN|(?:FOR\s+|,\s*)?ASOTIN\s+COUNTY"
    r"|COUNTY\s+OF\s+ASOTIN)",
    re.I,
)
_NOTICE_TO_CREDITORS = re.compile(r"\bNOTICE\s+TO\s+CREDITORS\b", re.I)
_DECEDENT = re.compile(
    r"(?:IN\s+RE\s+THE\s+ESTATE\s+OF|IN\s+THE\s+MATTER\s+OF\s+THE\s+ESTATE\s+OF|ESTATE\s+OF)"
    r"\s*:?\s*(?P<name>[A-Z][A-Za-z.,'/\- ]{2,120}?)\s*,?\s+Deceased\b",
    re.I,
)
# An alias the court caption adds ("JOHN DOE, a/k/a JACK DOE"): the first name is the
# decedent's legal name, the one the assessor would hold.
_ALIAS = re.compile(r"\s*,?\s*\b(?:A/?K/?A|ALSO\s+KNOWN\s+AS|F/?K/?A|N[EÉ]E)\b.*$", re.I)
_FIRST_PUBLISHED = re.compile(
    r"DATE\s+OF\s+FIRST\s+PUBLICATION\s*:?\s*(?P<d>[A-Z][a-z]+\.?\s+\d{1,2}\s*,\s*\d{4})", re.I)
# "Personal Representative: NAME" / "Co-Personal Representatives: A and B". Stops at the
# next labelled field. Not the "named below" sentence: that one has no colon.
# Every labelled field a notice prints, in any order: a value ends where the next begins.
_NEXT_LABEL = (
    r"(?=\s+(?:DATE\s+OF\s+FIRST\s+PUBLICATION|(?:CO-)?PERSONAL\s+REPRESENTATIVES?\s*:"
    r"|ATTORNEYS?\s+FOR|ADDRESS\s+FOR|DATED|COURT\s+OF|PRESENTED|/S/|SIGNED"
    r"|(?:CAUSE|CASE|PROBATE)\s+NO\b|SUPERIOR\s+COURT)|\s*$)"
)
_REPS = re.compile(
    r"(?:CO-)?PERSONAL\s+REPRESENTATIVES?\s*:\s*(?P<names>.+?)" + _NEXT_LABEL, re.I)
_CLAIMS_ADDRESS = re.compile(
    r"ADDRESS\s+FOR\s+MAILING\s*(?:OR|/|AND)?\s*(?:SERVICE|NOTICE)?\s*:\s*(?P<addr>.+?)"
    + _NEXT_LABEL, re.I)
_NAME_SPLIT = re.compile(r"\s*(?:,\s*and\s+|\s+and\s+|;|,(?!\s*(?:JR|SR|II|III|IV)\b))\s*", re.I)
_PERSON_NAME = re.compile(r"^[A-Za-z][A-Za-z.,'\- ]{1,60}$")


@dataclass(frozen=True)
class ProbateNotice:
    cause_no: str
    decedent: str
    first_published: date
    personal_reps: tuple[str, ...] = field(default_factory=tuple)
    claims_address: str | None = None


def is_asotin_probate(text: str) -> bool:
    """An Asotin Superior Court probate notice to creditors (cause code AND court)."""
    return bool(_NOTICE_TO_CREDITORS.search(text) and _ASOTIN_CAUSE.search(text)
                and _ASOTIN_COURT.search(text))


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" ,.;")


def _parse_date(raw: str) -> date | None:
    raw = re.sub(r"\s*,\s*", ", ", raw.replace(".", ""))
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_probate_notice(text: str) -> ProbateNotice | None:
    """The notice's fields, or None when it is not an Asotin probate notice or a
    mandatory field (cause, decedent, first publication date) cannot be read."""
    if not is_asotin_probate(text):
        return None
    cause = _ASOTIN_CAUSE.search(text)
    dec = _DECEDENT.search(text)
    pub = _FIRST_PUBLISHED.search(text)
    if not (cause and dec and pub):
        return None
    decedent = _clean(_ALIAS.sub("", dec.group("name")))
    first_published = _parse_date(pub.group("d"))
    if not decedent or first_published is None:
        return None

    reps: list[str] = []
    # "Attorney for Co-Personal Representatives: <lawyer>" is the same label: skip it.
    m = next((r for r in _REPS.finditer(text)
              if not re.search(r"ATTORNEYS?\s+FOR\s+(?:THE\s+)?$", text[:r.start()], re.I)), None)
    if m:
        for part in _NAME_SPLIT.split(m.group("names")):
            part = _clean(part)
            if _PERSON_NAME.match(part) and len(part.split()) >= 2:
                reps.append(part)
    addr = _CLAIMS_ADDRESS.search(text)
    return ProbateNotice(
        cause_no=cause.group(1),
        decedent=decedent.upper(),
        first_published=first_published,
        personal_reps=tuple(r.upper() for r in reps),
        claims_address=_clean(addr.group("addr"))[:200] if addr else None,
    )
