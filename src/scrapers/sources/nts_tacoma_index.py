"""Tacoma Daily Index — Notice of Trustee Sale (NTS) parser, Pierce County.

WA law (RCW 61.24.040) requires every NTS to be published in full in a county
legal newspaper. The Tacoma Daily Index publishes Pierce County NTS notices as
free, open (robots.txt: Disallow nothing) HTML pages under
/category/legal-notices/. Each carries the auction date / default amount /
trustee / TS# that the county recorder search grid (our current pre_foreclosure
source) never exposes — exactly the fields a foreclosure investor acts on.

This module is the PARSER: pure functions that turn one notice's plain-text body
into a structured dict. The crawler (fetch the dated listing) and the matcher
(attach onto our existing Pierce pre_foreclosure Results by address/parcel) are
separate, later units. The WA NTS body is statutorily structured (labeled header
fields + Roman-numeral sections I-X), so label/section regex is reliable.

No network here — the input is already-fetched text, so this is fully unit-tested
against a real saved notice (tests/fixtures/nts_tacoma_*.txt), no mocks.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

# ── Labeled header fields ("Label: value" on their own line) ────────────────────
# Line-bounded ([^:\n] / [^\n]): a negated class matches newlines, so an unbounded
# label regex would jump across lines (e.g. "TRUSTEE'S SALE\nPublished 1:30 am…"
# matching the trustee label up to the time's colon). TS# is pinned to its YY-NNNN
# numeric shape so the title line "TS #: 25-76127-NOTICE OF TRUSTEE'S SALE" yields
# "25-76127", not "25-76127-NOTICE".
_TS_NUMBER = re.compile(r"TS\s*#\s*:\s*(\d{2}-\d{3,7})", re.I)
_TITLE_ORDER = re.compile(r"Title\s*Order\s*#\s*:\s*([\w\-]+)", re.I)
_GRANTOR = re.compile(r"^Grantor\s*:\s*([^\n]+)", re.I | re.M)
_BENEFICIARY = re.compile(r"^(?:Current\s+)?beneficiary[^:\n]*:\s*([^\n]+)", re.I | re.M)
_TRUSTEE = re.compile(r"^(?:Current\s+)?trustee[^:\n]*:\s*([^\n]+)", re.I | re.M)
_SERVICER = re.compile(r"^(?:Current\s+)?mortgage\s+servicer[^:\n]*:\s*([^\n]+)", re.I | re.M)
_DEED_REF = re.compile(r"Reference\s+number\s+of\s+the\s+deed\s+of\s+trust\s*:\s*([\w\-]+)", re.I)
_PARCEL = re.compile(r"Parcel\s+Number\(?s?\)?\s*:\s*([\w\-]+)", re.I)

# ── Auction: "will on 7/10/2026, at 10:00 AM at <location> sell at public auction"
_AUCTION = re.compile(
    r"will\s+on\s+(\d{1,2}/\d{1,2}/\d{4})\s*,?\s*at\s+([\d:]+\s*[AP]M)\s+at\s+(.+?)\s+sell\s+at\s+public\s+auction",
    re.I | re.S,
)
# ── Property: "Commonly known as: 19012 160TH ST EAST\nBONNEY LAKE, WASHINGTON 98391"
_COMMONLY_KNOWN = re.compile(r"Commonly\s+known\s+as\s*:\s*(.+?)(?:\n\s*which\s+is\s+subject|\n\s*II\.|\Z)", re.I | re.S)
# ── Section IV: "The sum owing on the obligation ... is: Principal $185,895.06"
_PRINCIPAL_OWING = re.compile(r"sum\s+owing\s+on\s+the\s+obligation[^$]*?Principal\s*\$?([\d,]+\.\d{2})", re.I | re.S)
# ── "Note Amount: $234,533.00" (original loan size)
_NOTE_AMOUNT = re.compile(r"Note\s+Amount\s*:?\s*\$?([\d,]+\.\d{2})", re.I)
# ── NOD transmittal date ("by both first class and certified mail on 1/20/2026")
_NOD_DATE = re.compile(r"certified\s+mail\s+on\s+(\d{1,2}/\d{1,2}/\d{4})", re.I)


def _first(pattern: re.Pattern, text: str) -> str | None:
    m = pattern.search(text)
    if not m:
        return None
    val = " ".join(m.group(1).split()).strip().rstrip(".,")
    return val or None


def _money(pattern: re.Pattern, text: str) -> Decimal | None:
    m = pattern.search(text)
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _clean_address(raw: str | None) -> str | None:
    """Collapse the multi-line 'Commonly known as' block into one address line."""
    if not raw:
        return None
    # Drop a trailing 'SITUATE IN THE COUNTY...' preamble that sometimes precedes it.
    addr = re.sub(r"^.*?(?=\d)", "", raw, count=1, flags=re.S) if re.search(r"\d", raw) else raw
    addr = " ".join(addr.split()).strip().rstrip(".,")
    addr = re.sub(r"\bWASHINGTON\b", "WA", addr, flags=re.I)
    return addr or None


def parse_nts_notice(text: str) -> dict[str, Any]:
    """Parse one Tacoma Daily Index NTS notice body into structured fields.

    Returns a dict with the investor-critical fields; any field not found is None.
    `ts_number` + `auction_date` are the load-bearing ones (the match key + the
    headline signal); a notice missing BOTH is almost certainly not an NTS body
    and the caller should discard it (see is_valid_nts).
    """
    # Normalize curly apostrophes/quotes the CMS emits so labels match.
    text = text.replace("’", "'").replace("‘", "'").replace("�", "'")

    auction_date = auction_time = auction_location = None
    am = _AUCTION.search(text)
    if am:
        auction_date = am.group(1).strip()
        auction_time = " ".join(am.group(2).split())
        auction_location = " ".join(am.group(3).split()).strip().rstrip(".,")

    return {
        "ts_number": _first(_TS_NUMBER, text),
        "title_order": _first(_TITLE_ORDER, text),
        "grantor": _first(_GRANTOR, text),
        "beneficiary": _first(_BENEFICIARY, text),
        "trustee": _first(_TRUSTEE, text),
        "servicer": _first(_SERVICER, text),
        "deed_reference": _first(_DEED_REF, text),
        "parcel": _first(_PARCEL, text),
        "auction_date": auction_date,
        "auction_time": auction_time,
        "auction_location": auction_location,
        "property_address": _clean_address(_first(_COMMONLY_KNOWN, text)),
        # Section IV "sum owing ... Principal" is the headline default/payoff figure.
        "principal_owing": _money(_PRINCIPAL_OWING, text),
        "note_amount": _money(_NOTE_AMOUNT, text),
        "nod_date": _first(_NOD_DATE, text),
    }


def is_valid_nts(parsed: dict[str, Any]) -> bool:
    """A parsed notice is usable only if it has a TS# AND an auction date.

    Those two anchor everything downstream (TS# = the trustee's stable key for the
    file; auction_date = the urgency signal + the freshness gate). A page that
    yields neither is site chrome or a non-NTS legal notice — discard it.
    """
    return bool(parsed.get("ts_number")) and bool(parsed.get("auction_date"))
