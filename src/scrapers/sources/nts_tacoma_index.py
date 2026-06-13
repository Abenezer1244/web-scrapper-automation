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

import hashlib
import html as _html
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

BASE_URL = "https://www.tacomadailyindex.com"
LEGAL_NOTICES_PATH = "/category/legal-notices/"
SOURCE = "tacoma_daily_index"
# Pierce County — every notice this paper carries is a Pierce trustee sale.
COUNTY = "pierce"
STATE = "WA"

# Notice URLs: /YYYY/MM/DD/ts-<num>-notice-of-trustees-sale/ (apostrophe stripped).
_NOTICE_HREF = re.compile(
    r'href="(https?://[^"]*?/\d{4}/\d{2}/\d{2}/[^"]*?notice-of-trustee[^"]*?)"',
    re.I,
)
_ARTICLE = re.compile(r"<article[^>]*>(.*?)</article>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)

# ── Labeled header fields ("Label: value" on their own line) ────────────────────
# Line-bounded ([^:\n] / [^\n]): a negated class matches newlines, so an unbounded
# label regex would jump across lines (e.g. "TRUSTEE'S SALE\nPublished 1:30 am…"
# matching the trustee label up to the time's colon).
#
# TS#: accept the label variants different trustees use (TS #, T.S. No., Trustee
# Sale No./Number) and the value formats that vary by trustee — North Star YY-NNNNN,
# Quality Loan/Clear Recon prefixed/suffixed (WA-25-…, …-WA, F25-…). Captured
# greedily but stopped before a trailing "-NOTICE…" (the page-title line concatenates
# "TS #: 25-76127-NOTICE OF TRUSTEE'S SALE") and at whitespace/EOL (Codex P1).
_TS_NUMBER = re.compile(
    r"(?:T\.?S\.?\s*#|T\.?S\.?\s*No\.?|Trustee\s+Sale\s+(?:No\.?|Number))\s*:?\s*"
    r"([A-Za-z0-9][A-Za-z0-9\-]*?)(?=\s|$|-?NOTICE\b)",
    re.I,
)
_TITLE_ORDER = re.compile(r"Title\s*Order\s*#\s*:\s*([\w\-]+)", re.I)
_GRANTOR = re.compile(r"^Grantor\s*:\s*([^\n]+)", re.I | re.M)
_BENEFICIARY = re.compile(r"^(?:Current\s+)?beneficiary[^:\n]*:\s*([^\n]+)", re.I | re.M)
# Negative lookahead so "Trustee Sale No.:" / "Trustee's Sale Number:" lines don't
# match as the trustee NAME (Codex P2) — only the real "…trustee…:" label does.
_TRUSTEE = re.compile(
    r"^(?:Current\s+)?trustee(?!\s*(?:'s)?\s+sale\s+(?:no|number))[^:\n]*:\s*([^\n]+)",
    re.I | re.M,
)
_SERVICER = re.compile(r"^(?:Current\s+)?mortgage\s+servicer[^:\n]*:\s*([^\n]+)", re.I | re.M)
_DEED_REF = re.compile(r"Reference\s+number\s+of\s+the\s+deed\s+of\s+trust\s*:\s*([\w\-]+)", re.I)
_PARCEL = re.compile(r"Parcel\s+Number\(?s?\)?\s*:\s*([\w\-]+)", re.I)

# ── Auction: "will on 7/10/2026, at 10:00 A.M. at <location> sell at public auction"
# Accept AM / A.M. / a.m. (dotted, Codex P1) and a multi-line location (.+? with re.S).
_AUCTION = re.compile(
    r"will\s+on\s+(\d{1,2}/\d{1,2}/\d{4})\s*,?\s*at\s+(\d{1,2}:\d{2}\s*[AP]\.?M\.?)\s+at\s+"
    r"(.+?)\s+sell\s+at\s+public\s+auction",
    re.I | re.S,
)
# ── Property: "Commonly known as: 19012 160TH ST EAST\nBONNEY LAKE, WASHINGTON 98391"
# Stop at "which is subject" / section II whether on the SAME line or the next
# (Codex P1: a one-line CMS wrap would otherwise swallow the deed/legal body).
_COMMONLY_KNOWN = re.compile(
    r"Commonly\s+known\s+as\s*:\s*(.+?)(?=\s+which\s+is\s+subject|\s+II\.\s|\n\n|\Z)",
    re.I | re.S,
)
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
    """Collapse the multi-line 'Commonly known as' block into one address line.

    Does NOT strip leading non-digit text (Codex P2): a legitimate unit prefix like
    'UNIT B 123 MAIN ST' must survive — stripping to the first digit would collapse
    distinct units to the same address and mis-attach auction data. We only collapse
    whitespace + normalize the state word; _COMMONLY_KNOWN already starts the capture
    after the label, so there is no preamble to remove.
    """
    if not raw:
        return None
    addr = " ".join(raw.split()).strip().rstrip(".,")
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


# ── Crawl helpers (pure: extraction + transform; the worker injects the fetcher) ──

def extract_notice_urls(listing_html: str) -> list[str]:
    """Pull distinct NTS-notice URLs from a /category/legal-notices/ listing page.

    Order-preserving dedupe (a notice can appear twice on a listing). The regex
    targets the dated trustee-sale URL shape, so non-NTS legal notices on the same
    page (probate, name changes, …) are skipped.
    """
    seen: list[str] = []
    for m in _NOTICE_HREF.finditer(listing_html):
        url = _html.unescape(m.group(1))
        if url not in seen:
            seen.append(url)
    return seen


def extract_article_text(notice_html: str) -> str:
    """Strip a notice page down to its article body plain text for the parser."""
    m = _ARTICLE.search(notice_html)
    body = m.group(1) if m else notice_html
    body = _SCRIPT_STYLE.sub(" ", body)
    body = _TAGS.sub("\n", body)
    body = _html.unescape(body)
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    return "\n".join(lines)


def _to_date(mdy: str | None) -> date | None:
    """Parse an M/D/YYYY auction date string to a date; None if unparseable."""
    if not mdy:
        return None
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\b", mdy)
    if not m:
        return None
    mm, dd, yyyy = (int(g) for g in m.groups())
    try:
        return date(yyyy, mm, dd)
    except ValueError:
        return None


def notice_to_row(parsed: dict[str, Any], source_url: str, today: date) -> dict[str, Any] | None:
    """Transform a parsed notice into an nts_notices upsert row, or None if unusable.

    Adds the normalized match key (address_intel.address_match_key — the SAME key
    the matcher computes for a lead), an is_active flag (False once the auction is
    in the past), and a content hash for dedup / source-drift detection. Returns
    None for non-NTS pages (is_valid_nts) so the caller skips them.
    """
    if not is_valid_nts(parsed):
        return None
    from src.utils.address_intel import address_match_key

    auction = _to_date(parsed.get("auction_date"))
    addr = parsed.get("property_address")
    norm = address_match_key(addr)
    # Hash the load-bearing parsed fields so a re-crawl of an unchanged notice is a
    # no-op and a source/parser change is observable.
    payload = "|".join(
        str(parsed.get(k) or "")
        for k in ("ts_number", "auction_date", "property_address", "trustee",
                  "beneficiary", "principal_owing", "parcel")
    )
    raw_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return {
        "source": SOURCE,
        "ts_number": parsed["ts_number"],
        "county": COUNTY,
        "state": STATE,
        "parcel": parsed.get("parcel"),
        "property_address": addr,
        "property_address_normalized": norm,
        "auction_date": auction,
        "auction_time": parsed.get("auction_time"),
        "auction_location": parsed.get("auction_location"),
        "grantor": parsed.get("grantor"),
        "trustee": parsed.get("trustee"),
        "beneficiary": parsed.get("beneficiary"),
        "principal_owing": parsed.get("principal_owing"),
        "note_amount": parsed.get("note_amount"),
        "nod_date": parsed.get("nod_date"),
        "source_url": source_url,
        "raw_hash": raw_hash,
        "is_active": bool(auction and auction >= today),
    }
