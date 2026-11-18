"""Douglas County recorded Notices of Trustee's Sale, read from the recorder's free
detail page (the recording's image is sold, not shown; live 2026-11-17).

Douglas's AcclaimWeb detail page carries what an auction lead needs, typed by the
recorder: Doc Type ("NTS - NOTICE OF TRUSTEE SALE"), Auditor File Number (the
instrument), Record Date, Grantor (the borrowers, one <span> each), Grantee (the
beneficiary and the trustee), Related DocLink ("3227557 [DT]", the deed of trust; on a
discontinuance, the notice it ends) and APN (11 digits). Its Remarks field is the
recorder's typed opening of the notice, and states the sale:

    NOTICE IS HEREBY GIVEN THAT <TRUSTEE>, THE UNDERSIGNED TRUSTEE, WILL ON 3/19/2027,
    AT 9:00AM AT THE FRONT ENTRANCE OF THE DOUGLAS COUNTY ANNEX BUILDING ... SELL AT
    PUBLIC AUCTION ...

Remarks are free text ("DECEMBER 4TH 2026", "FEBRUARY 5,2027", "AT 10:00AM ON
4/2/2027", "SALE TO HAPPEN JANUARY 29, 2027"), so a sale date is accepted only when it
cannot be anything else: the remarks speak of a sale, name exactly one date, and that
date is a lawful sale day (RCW 61.24.040: a Friday, or the Monday after a Friday legal
holiday) within the notice's reach of its recording. Anything else is held, not guessed.

A notice naming several parcels is held (which property a lead is about is not ours to
pick), and an amended notice is held for a person: superseding a live notice from a
shared deed of trust alone is not safe (Codex). NOTICE OF CONTINUANCE is not read: in
Douglas it is the current-use classification continuance (RCW 84.34), no remarks, often
many parcels.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta
from typing import Any

from bs4 import BeautifulSoup

SOURCE = "douglas_recorder"
COUNTY = "douglas"
STATE = "WA"

# The recorder's own descriptions (doc-type list, live 2026-11-15) -> the code we file.
NOTICE_CODES = {"NTS": "NOTICE OF TRUSTEE SALE", "ANTS": "AMENDED NOTICE TRUSTEE SALE",
                "NDTS": "NOTICE OF DISCONTINUANCE"}

_APN_RE = re.compile(r"^\d{11}$")
_INSTRUMENT_RE = re.compile(r"^\d{5,10}$")
_MONTHS = ("JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
           "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER")
_WORD_DATE_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:ST|ND|RD|TH)?\s*,?\s*(\d{4})\b")
_NUM_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})\b")
_TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\s*(?:O'?CLOCK\s*)?([AP])\.?\s*M\b")
_SALE_WORDS_RE = re.compile(r"\b(?:SELL|SALE|AUCTION)\b")
# The ways the recorder introduces the SALE date (every live form, 2026-08..11):
#   "WILL ON <date>", "GIVEN THAT ON [FRIDAY] <date>", "SALE TO HAPPEN <date>",
#   "AT <time> ON <date> SALE ...". The date must be the one these introduce.
_ANY_DATE = (r"(?:(?:" + "|".join(_MONTHS) + r")\s+\d{1,2}(?:ST|ND|RD|TH)?\s*,?\s*\d{4}"
             r"|\d{1,2}/\d{1,2}/(?:\d{4}|\d{2}))")
_WEEKDAY = r"(?:(?:MONDAY|FRIDAY),?\s+)?"
# "WILL ON"/"THAT ON" name the sale only when the sentence goes on to the statutory
# "SELL AT PUBLIC AUCTION" (every live notice does); "INSPECTION WILL ON ..." does not.
_SALE_CONSTRUCTION_RE = re.compile(
    r"\b(?:WILL|THAT)\s+ON\s+" + _WEEKDAY + _ANY_DATE + r"\b.{0,400}?\bSELL\s+AT\s+PUBLIC\s+AUCTION\b"
    + r"|\bSALE\s+TO\s+HAPPEN\s+(?:ON\s+)?" + _WEEKDAY + _ANY_DATE
    + r"|^AT\s+\d{1,2}:\d{2}\s*[AP]\.?M\.?\s+ON\s+" + _ANY_DATE + r"\s+SALE\b", re.S)
# The notice must sit between recording and sale: RCW 61.24.040 asks for at least 90
# days; a year bounds a typo ("2207") without rejecting a long continuance.
_MIN_LEAD_DAYS = 30
_MAX_LEAD_DAYS = 365


# The crawl re-reads every recording that could still name an upcoming sale.
WINDOW_DAYS = _MAX_LEAD_DAYS
# A discontinuance's Related DocLink names the notice it ends ("[NTS]", or untyped), and
# also the deed of trust and other documents (live: NTS 7, untyped 5, DT 5, MOAG 1).
_CANCEL_LINK_TYPES = {"NTS", "ANTS", None}


_RELATED_RE = re.compile(r"(\d{5,10})(?:\s*\[([A-Z0-9]{1,12})\])?")


def discontinued(ndts: list[dict[str, Any]]) -> frozenset[str]:
    """The notice instruments the parsed discontinuances end.

    Raises ValueError for a discontinuance with an unreadable link or none that can name
    a notice: which sale it ends is then unknown, and no notice may be published while
    one of them could be the cancelled one (Codex)."""
    out: set[str] = set()
    for p in ndts:
        links = p.get("related", [])
        if any(kind == "UNREADABLE" for _, kind in links):
            raise ValueError(f"discontinuance {p.get('instrument')} has an unreadable related link")
        ends = {inst for inst, kind in links if kind in _CANCEL_LINK_TYPES}
        if not ends:
            raise ValueError(f"discontinuance {p.get('instrument')} names no notice")
        out |= ends
    return frozenset(out)


def ts_key(instrument: str) -> str:
    """The nts_notices key: the RECORDED document (as Benton's), not the sale."""
    return f"REC-{instrument}"


def _value(row) -> list[str]:
    """One detail row's values: its <span>s when it lists several, else its text."""
    box = row.find("div", class_=["listDocDetails", "formInput"])
    if box is None:
        return []
    spans = box.find_all("span")
    if row.find("a") is not None and not spans:
        # Every link, a blank one included: a blank label is unreadable, not absent (Codex).
        return [" ".join(a.get_text(" ").split()) for a in box.find_all("a")]
    vals = [s.get_text(" ") for s in spans] if spans else [box.get_text(" ")]
    return [" ".join(v.split()) for v in vals if v and v.strip()]


def parse_detail(html: str) -> dict[str, Any] | None:
    """A recording's detail page -> its fields, or None when it is not a detail page."""
    soup = BeautifulSoup(html or "", "html.parser")
    fields: dict[str, list[str]] = {}
    for row in soup.find_all("div", class_="docDetailRow"):
        label = row.find("div", class_="detailLabel")
        if label is not None:
            fields[" ".join(label.get_text(" ").split()).rstrip(":").strip()] = _value(row)
    doc_type = (fields.get("Doc Type") or [""])[0]
    instrument = (fields.get("Auditor File Number") or [""])[0]
    recorded = (fields.get("Record Date") or [""])[0]
    if not doc_type or not _INSTRUMENT_RE.match(instrument):
        return None
    try:
        record_date = datetime.strptime(recorded, "%m/%d/%Y").date()
    except ValueError:
        return None
    related = []
    for v in fields.get("Related DocLink", []):
        m = _RELATED_RE.fullmatch(v)
        # A label we cannot read whole is kept as unreadable, never as an untyped link.
        related.append((m.group(1), m.group(2)) if m else (None, "UNREADABLE"))
    return {
        "instrument": instrument,
        "record_date": record_date,
        "doc_code": doc_type.split(" - ")[0].strip().upper(),
        "doc_type": doc_type,
        "grantors": fields.get("Grantor", []),
        "grantees": fields.get("Grantee", []),
        "remarks": " ".join(fields.get("Remarks", [])),
        "related": related,
        "apns": list(dict.fromkeys(a for v in fields.get("APN", []) for a in re.findall(r"\d[\d\-]*\d", v))),
    }


def _holidays(year: int) -> set[date]:
    """Washington legal holidays that can land on a Friday or a Monday (RCW 1.16.050):
    the fixed-date ones as observed (Saturday -> the Friday before, Sunday -> the Monday
    after), the day after Thanksgiving, and the Monday holidays."""
    def observed(d: date) -> date:
        return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d

    def nth_monday(month: int, n: int) -> date:
        first = date(year, month, 1)
        return first + timedelta(days=(0 - first.weekday()) % 7 + 7 * (n - 1))

    def last_monday(month: int) -> date:
        d = date(year, month + 1, 1) - timedelta(days=1)
        return d - timedelta(days=d.weekday())

    out = {observed(date(y, m, d)) for y in (year, year + 1) for m, d in
           ((1, 1), (6, 19), (7, 4), (11, 11), (12, 25))}
    nov1 = date(year, 11, 1)
    thanksgiving = nov1 + timedelta(days=(3 - nov1.weekday()) % 7 + 21)
    out |= {thanksgiving + timedelta(days=1), nth_monday(1, 3), nth_monday(2, 3), last_monday(5),
            nth_monday(9, 1)}
    return out


def is_sale_day(d: date) -> bool:
    """RCW 61.24.040: a Friday that is not a legal holiday, or the Monday after a Friday
    holiday (when that Monday is not a holiday itself)."""
    hol = _holidays(d.year) | _holidays(d.year - 1)
    if d.weekday() == 4:
        return d not in hol
    return d.weekday() == 0 and d not in hol and (d - timedelta(days=3)) in hol


def parse_sale(remarks: str, record_date: date) -> tuple[date | None, str | None, str]:
    """(sale date, time, outcome) from the typed remarks. outcome is "found" or why not."""
    text = " ".join((remarks or "").upper().split())
    if not text:
        return None, None, "no_remarks"
    if not _SALE_WORDS_RE.search(text):
        return None, None, "no_sale_language"
    dates: set[date] = set()
    for mon, day, year in _WORD_DATE_RE.findall(text):
        try:
            dates.add(date(int(year), _MONTHS.index(mon) + 1, int(day)))
        except ValueError:
            return None, None, "impossible_date"
    for mon, day, year in _NUM_DATE_RE.findall(text):
        y = int(year) + (2000 if len(year) == 2 else 0)
        try:
            dates.add(date(y, int(mon), int(day)))
        except ValueError:
            return None, None, "impossible_date"
    if len(dates) != 1:
        return None, None, "no_date" if not dates else "several_dates"
    if not _SALE_CONSTRUCTION_RE.search(text):
        # A sale word and a date are not enough ("SALE DATE TBD; INSPECTION ON 2/5/2027").
        return None, None, "no_sale_construction"
    sale = dates.pop()
    lead = (sale - record_date).days
    if not _MIN_LEAD_DAYS <= lead <= _MAX_LEAD_DAYS:
        return None, None, "date_out_of_reach"
    if not is_sale_day(sale):
        return None, None, "not_a_sale_day"
    times = {f"{t} {ap}M" for t, ap in _TIME_RE.findall(text)}
    return sale, (times.pop() if len(times) == 1 else None), "found"


def notice_to_row(parsed: dict[str, Any], source_url: str, today: date,
                  cancelled: frozenset[str] = frozenset()) -> tuple[dict[str, Any] | None, str]:
    """A parsed NTS detail -> (nts_notices upsert row, "ok"), or (None, why it is held)."""
    if parsed.get("doc_code") != "NTS":
        return None, "not_an_original_notice"
    apns = list(dict.fromkeys(a.replace("-", "") for a in parsed.get("apns") or []))
    if len(apns) != 1:
        return None, "several_parcels" if apns else "no_parcel"
    if not _APN_RE.match(apns[0]):
        return None, "parcel_shape"
    sale, sale_time, outcome = parse_sale(parsed.get("remarks", ""), parsed["record_date"])
    if sale is None:
        return None, outcome
    instrument = parsed["instrument"]
    grantor = " / ".join(parsed.get("grantors") or []) or None
    payload = "|".join(str(v or "") for v in (instrument, sale, sale_time, apns[0], grantor,
                                                parsed.get("remarks")))
    return {
        "source": SOURCE,
        "ts_number": ts_key(instrument),
        "county": COUNTY,
        "state": STATE,
        "parcel": apns[0],
        "property_address": None,
        "property_address_normalized": None,
        "auction_date": sale,
        "auction_time": sale_time,
        "auction_location": None,
        "grantor": grantor[:512] if grantor else None,
        "trustee": None,
        "beneficiary": None,
        "principal_owing": None,
        "principal_owing_basis": None,
        "note_amount": None,
        "nod_date": None,
        "source_url": source_url[:512],
        "raw_hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "is_active": sale >= today and instrument not in cancelled,
    }, "ok"
