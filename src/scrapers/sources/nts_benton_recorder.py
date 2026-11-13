"""Benton County recorded Notices of Trustee's Sale, read from the recorder's own image.

Benton's EagleWeb index truncates the legal description, so most recorded NTS
carry no usable parcel in the index. The recorded document itself does: the
recorder serves each image as a PDF on its public site (online viewing is free;
fees apply only to official/certified copies), and the PDF has a text layer.

That text layer drops spaces inside phrases ("CurrentTrusteeoftheDeed ofTrust:",
"sum owingon theobligation"), so labels are matched whitespace-insensitively and
values are read from the original lines. Nothing is repaired by guessing: OCR
sometimes reads the slashes of a date as digits ("21512027" for 2/5/2027), and
such a value is left unparsed.

RCW 61.24.040 fixes the notice's form, which is what makes this parseable:
  heading     "<instrument> NTS|NTSCL" + recording date/time
  TS No       the trustee's sale number (not on every notice)
  Grantor / Current Trustee / Current Beneficiary
  Parcel      "Parcel Number(s)", "APN:", "Tax Parcel #:" (Benton: 15 digits, often
              printed 1-1880-305-0002-002)
  Section I   the sale: "... will on <date>, at <time>, <place> ..."
  address     "commonly known as <street>[ newline <city>, WA <zip>]"
  Section IV  "The sum owing on the obligation ... is: ... principal ... $X"

A notice that names more than one property (several parcels, or parcels in
another county) is parsed but flagged ``multi_property``; callers must not pin
it to one parcel.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

SOURCE = "benton_recorder"
COUNTY = "benton"
STATE = "WA"

_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")
_MONTH_RE = "|".join(_MONTHS)


def _loose(phrase: str) -> str:
    """A regex for ``phrase`` that tolerates missing or extra whitespace anywhere."""
    return r"\s*".join(re.escape(ch) for ch in phrase.replace(" ", ""))


_HEADING = re.compile(r"^\s*(\d{4}-\d{6})\s+(NTSCL|NTS)(?![A-Z])", re.M)
_RECORDED_AT = re.compile(r"(\d{2}/\d{2}/\d{4})\s+\d{2}:\d{2}:\d{2}\s*[AP]M")
_TS_NO = re.compile(
    r"(?:" + _loose("TS No") + r"\.?|" + _loose("TS #") + r"|" + _loose("Trustee Sale #")
    + r"|" + _loose("File No") + r")\s*:?\s*([A-Z0-9][A-Z0-9\-]{4,})",
    re.I,
)
# "Grantor:", "Grantor(s):", "Grantor(s) for Recording Purposes under RCW 65.04.015:",
# "Grantor(s) of Deed of Trust <name>" (no colon).
_GRANTOR = re.compile(
    r"^\s*" + _loose("Grantor") + r"\s*(?:\(\s*s\s*\))?"
    r"(?:[^:\n]{0,60}:|\s*" + _loose("of Deed of Trust") + r")\s*(.+)$",
    re.I | re.M,
)
# The label runs to the first colon on its line ("Current Beneficiary(ies):",
# "... of the Deed of Trust and Grantee (for Recording Purposes ...):"); a line with no
# colon prints the label then the value ("Current Trustee Affinia Default Services").
_LABEL_TAIL = r"(?:[^:\n]{0,140}:|(?![^\n]*:)(?:\s*" + _loose("of the Deed of Trust") + r")?)\s*(.+)$"
_TRUSTEE = re.compile(r"^\s*" + _loose("Current Trustee") + _LABEL_TAIL, re.I | re.M)
_BENEFICIARY = re.compile(r"^\s*" + _loose("Current Beneficiary") + _LABEL_TAIL, re.I | re.M)
# A wrapped name continues on the next line unless that line opens another field.
_NEXT_FIELD = re.compile(
    r"^\s*(?:Current|Original|Grantee|Deed|Parcel|Title|Reference|Loan|TS\b|APN|Abbreviated|I\.)", re.I
)
# Parcel labels seen on Benton notices. The value is digits with optional dashes.
_PARCEL_LABEL = re.compile(
    r"(?:" + "|".join([_loose("Tax Parcel"), _loose("Parcel Number"), _loose("Parcel No"),
                       _loose("Parcel ID"), r"\bAPN", _loose("Assessor's Parcel")]) + r")"
    r"[^0-9\n]{0,24}?(?=\d)",
    re.I,
)
# One parcel-shaped number (dashes allowed, no spaces: spaces would merge neighbours).
_PARCEL_NUMBER = re.compile(r"\d[\d\-]{9,24}\d")
_GROUPED_NUMBER = re.compile(r"\d[\d\- ]{9,30}\d")
_LIST_SEPARATOR = re.compile(r"[,;&]|\band\b", re.I)
# A continuation line that only lists more numbers ("128993050001007, and").
_NUMBERS_ONLY = re.compile(r"^[\d\-\s,;&]*(?:and)?[\d\-\s,;&]*$", re.I)
_COMMONLY_KNOWN = re.compile(_loose("commonly known as") + r"\s*:?\s*(.+)(?:\n(.+))?", re.I)
# Section I: "... will on <date>, at <time>" or "... that on <date>, <time>".
_SALE = re.compile(
    r"(?:" + _loose("will on") + "|" + _loose("that on") + r")\s*"
    rf"((?:{_MONTH_RE})\s*\d{{1,2}}\s*,\s*\d{{4}}|\d{{1,2}}/\d{{1,2}}/\d{{4}})(?!\d)"
    r"\s*,?\s*(?:at\s*)?(\d{1,2}:\d{2}\s*[AP]\.?\s*M\.?)?",
    re.I,
)
_SECTION_IV = re.compile(_loose("sum owing on the obligation"), re.I)
_SECTION_V = re.compile(r"\bV\s*[.,]\s*(?=[A-Z])")
_PRINCIPAL = re.compile(r"principal[^$]{0,60}?\$\s*([\d,]+\.\d{2})", re.I | re.S)
_FIRST_DOLLAR = re.compile(r"\$\s*([\d,]+\.\d{2})")
_OTHER_COUNTY = re.compile(r"\b(?!Benton\b)([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s+County\s*:", re.M)
_COMMERCIAL = re.compile(_loose("commercial loan"), re.I)
_ZIP = re.compile(r"\bWA(?:SHINGTON)?\s*,?\s*(\d{5})(?:-\d{4})?", re.I)


def benton_parcel(raw: str | None) -> str | None:
    """A Benton parcel as the county indexes it: 15 digits starting with 1, or None."""
    digits = re.sub(r"\D", "", raw or "")
    return digits if len(digits) == 15 and digits.startswith("1") else None


def _clean(value: str | None, limit: int = 255) -> str | None:
    if not value:
        return None
    value = re.sub(r"\s+", " ", value).strip(" ,;:")
    return value[:limit] or None


def _to_date(text: str | None) -> date | None:
    if not text:
        return None
    try:
        numeric = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text.strip())
        if numeric:
            return date(int(numeric.group(3)), int(numeric.group(1)), int(numeric.group(2)))
        m = re.match(rf"({_MONTH_RE})\s*(\d{{1,2}})\s*,\s*(\d{{4}})", text, re.I)
        if m:
            return date(int(m.group(3)), _MONTHS.index(m.group(1).title()) + 1, int(m.group(2)))
    except ValueError:
        return None
    return None


def _address(text: str) -> str | None:
    """The 'commonly known as' street address, through the WA zip."""
    m = _COMMONLY_KNOWN.search(text)
    if not m:
        return None
    line = m.group(1)
    if not _ZIP.search(line) and m.group(2):
        line = f"{line}, {m.group(2)}"  # street on one line, "CITY, WA 99336" on the next
    zm = _ZIP.search(line)
    if zm is None:
        return None  # no zip: the address is incomplete in the text
    return _clean(line[:zm.end()])


def _section_iv(text: str) -> tuple[Decimal | None, str | None]:
    """Section IV sum owing: (amount, basis). 'principal' only when labelled so."""
    m = _SECTION_IV.search(text)
    if not m:
        return None, None
    span = text[m.end():m.end() + 700]
    end = _SECTION_V.search(span)
    span = span[:end.start()] if end else span
    labelled = _PRINCIPAL.search(span)
    if labelled:
        return Decimal(labelled.group(1).replace(",", "")), "principal"
    first = _FIRST_DOLLAR.search(span)
    if first:
        return Decimal(first.group(1).replace(",", "")), "sum_owing"
    return None, None


def _name(pattern: re.Pattern, text: str, max_lines: int = 4) -> str | None:
    """A labelled name, joined with its wrapped continuation lines (up to the next
    field, a line with a colon, or a blank line)."""
    m = pattern.search(text)
    if not m:
        return None
    parts = [m.group(1)]
    # [1:]: the match ends at its own line's end; a blank line stops the name.
    for line in text[m.end():].split("\n")[1:max_lines + 1]:
        if not line.strip() or _NEXT_FIELD.match(line) or ":" in line:
            break
        parts.append(line)
    return _clean(" ".join(parts))


def _parcels(text: str) -> list[str]:
    """Every Benton parcel listed under a parcel label, including a list on one line
    ("A,B") and a list continued on the next line. Numbers that are not Benton-shaped
    (a PACS property id, another county's parcel) are not parcels here."""
    found: list[str] = []
    for m in _PARCEL_LABEL.finditer(text):
        lines = text[m.end():].split("\n")
        chunk = [lines[0]] + [ln for ln in lines[1:3] if ln.strip() and _NUMBERS_ONLY.match(ln)]
        # Split the list on its separators, then read each item both ways: as
        # unspaced numbers, and as one space-grouped number ("1 1098 402 0208 009").
        for item in _LIST_SEPARATOR.split(" ".join(chunk)):
            candidates = _PARCEL_NUMBER.findall(item)
            grouped = _GROUPED_NUMBER.search(item)
            if grouped:
                candidates.append(grouped.group(0))
            for raw in candidates:
                p = benton_parcel(raw)
                if p and p not in found:
                    found.append(p)
    return found


def parse_recorded_nts(text: str) -> dict[str, Any]:
    """Text of one recorded Benton NTS -> its facts. Missing facts are None."""
    heading = _HEADING.search(text)
    recorded = _RECORDED_AT.search(text)
    parcels = _parcels(text)
    other_counties = sorted(set(_OTHER_COUNTY.findall(text)))
    iv = _SECTION_IV.search(text)
    sale = _SALE.search(text[:iv.start()] if iv else text)  # Section I precedes Section IV
    amount, basis = _section_iv(text)
    ts = _TS_NO.search(text)
    return {
        "instrument_number": heading.group(1) if heading else None,
        "notice_code": heading.group(2) if heading else None,
        "recorded_date": datetime.strptime(recorded.group(1), "%m/%d/%Y").date() if recorded else None,
        "ts_number": ts.group(1) if ts else None,
        "commercial": bool(heading and heading.group(2) == "NTSCL") or bool(_COMMERCIAL.search(text)),
        "grantor": _name(_GRANTOR, text),
        "trustee": _name(_TRUSTEE, text),
        "beneficiary": _name(_BENEFICIARY, text),
        "parcels": parcels,
        "property_address": _address(text),
        "auction_date": _to_date(sale.group(1)) if sale else None,
        "auction_time": _clean(sale.group(2), 32) if sale and sale.group(2) else None,
        "principal_owing": amount,
        "principal_owing_basis": basis,
        "multi_property": len(parcels) > 1 or bool(other_counties),
        "other_counties": other_counties,
    }


def ts_key(instrument_number: str) -> str:
    """The nts_notices key for a recorded notice: the RECORDER document, not the sale.

    Many Benton notices print no TS number, and an amended notice is a new recorded
    document, so the instrument number is the stable identity (Codex)."""
    return f"REC-{instrument_number}"


def recorded_notice_to_row(parsed: dict[str, Any], source_url: str, today: date,
                           cancelled: frozenset[str] = frozenset()) -> dict[str, Any] | None:
    """A parsed recorded NTS -> an nts_notices upsert row, or None when it must not be
    an auction lead: no instrument, no sale date, several properties, or no single
    Benton parcel. ``cancelled`` holds instrument numbers a recorded Discontinuance
    names; such a notice is stored inactive so no lead is built from it."""
    import hashlib

    from src.utils.address_intel import address_match_key

    instrument = parsed.get("instrument_number")
    auction = parsed.get("auction_date")
    if not instrument or auction is None or parsed.get("multi_property") or len(parsed.get("parcels") or []) != 1:
        return None
    parcel = parsed["parcels"][0]
    addr = _clean(parsed.get("property_address"), 512)
    payload = "|".join(str(v or "") for v in (instrument, auction, addr, parsed.get("trustee"),
                                               parsed.get("beneficiary"), parsed.get("principal_owing"), parcel))
    return {
        "source": SOURCE,
        "ts_number": ts_key(instrument),
        "county": COUNTY,
        "state": STATE,
        "parcel": parcel,
        "property_address": addr,
        "property_address_normalized": _clean(address_match_key(addr), 512) if addr else None,
        "auction_date": auction,
        "auction_time": _clean(parsed.get("auction_time"), 16),
        "auction_location": None,
        "grantor": _clean(parsed.get("grantor"), 512),
        "trustee": _clean(parsed.get("trustee"), 255),
        "beneficiary": _clean(parsed.get("beneficiary"), 255),
        "principal_owing": parsed.get("principal_owing"),
        "principal_owing_basis": parsed.get("principal_owing_basis"),
        "note_amount": None,
        "nod_date": None,
        "source_url": source_url[:512],
        "raw_hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "is_active": auction >= today and instrument not in cancelled,
    }
