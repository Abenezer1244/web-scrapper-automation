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

# Notice URLs. The live slug format is /YYYY/MM/DD/ts-<wa-NN-NNNNN>-...-idx<N>/
# (trustee-sale, "ts-" prefix); an older format used …-notice-of-trustees-sale.
# The listing mixes other legal notices (probate "…-notice-to-creditors", etc.),
# so we match dated paths whose slug is a trustee sale: starts with "ts-" OR
# contains "trustee". is_valid_nts (TS# + auction_date) is the backstop that drops
# any non-NTS page that slips through. HOST-PINNED to tacomadailyindex.com (Codex
# P2) so a syndicated/compromised off-site link can't be crawled; the worker also
# passes same_origin_as=BASE_URL to safe_get.
_NOTICE_HREF = re.compile(
    r'href="(https?://(?:www\.)?tacomadailyindex\.com/\d{4}/\d{2}/\d{2}/'
    r'(?:ts-|[^"/]*trustee)[^"]*)"',
    re.I,
)
_ARTICLE = re.compile(r"<article[^>]*>(.*?)</article>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)

# ── Labeled header fields ─────────────────────────────────────────────────────
# Two NTS layouts seen: North Star (each label on its own line) and Quality Loan
# (the WHOLE header on ONE line: "Trustee Sale No.: X Title Order No.: Y Grantor(s)
# …: Z Current Beneficiary …: W Current Trustee …: V"). So label VALUES can't stop
# at a newline; they stop at the next KNOWN label (or section I/II, or end) via
# _STOP. This handles both layouts and never lets a value swallow the next field.
_STOP = (
    r"(?=\s+(?:Title\s+Order|Reference\s+Number|Parcel\s+Number|Grantor|"
    r"Current\s+Beneficiary|Current\s+Trustee|Current\s+(?:Loan\s+)?Mortgage|"
    r"which\s+is\s+subject|Subject\s+to\b|I\.\s*NOTICE|II\.)|\n|\Z)"
)

# TS#: label variants (TS #, T.S. No., Trustee Sale No./Number) + value formats that
# vary by trustee (North Star YY-NNNNN, Quality Loan WA-25-…-RM). Stop before a
# trailing "-NOTICE…" (title-line concat) and at whitespace/EOL (Codex P1).
_TS_NUMBER = re.compile(
    r"(?:T\.?S\.?\s*#|T\.?S\.?\s*No\.?|Trustee\s+Sale\s+(?:No\.?|Number))\s*:?\s*"
    r"([A-Za-z0-9][A-Za-z0-9\-]*?)(?=\s|$|-?NOTICE\b)",
    re.I,
)
_TITLE_ORDER = re.compile(r"Title\s*Order\s*(?:#|No\.?)\s*:\s*([\w\-]+)", re.I)
# Grantor / Grantor(s) [optional "for Recording Purposes …"] : value
_GRANTOR = re.compile(r"Grantor\(?s?\)?[^:\n]*:\s*(.+?)" + _STOP, re.I | re.S)
_BENEFICIARY = re.compile(r"(?:Current\s+)?Beneficiary[^:\n]*:\s*(.+?)" + _STOP, re.I | re.S)
# Require the precise "Trustee of the Deed of Trust:" label (both layouts use it),
# so neither "Trustee Sale No.:" nor a prose "the undersigned Trustee," is read as
# the trustee NAME (Codex P2 + the one-line-layout 09:00-time-colon trap).
_TRUSTEE = re.compile(
    r"(?:Current\s+)?Trustee\s+of\s+the\s+Deed\s+of\s+Trust\s*:\s*(.+?)" + _STOP,
    re.I | re.S,
)
_SERVICER = re.compile(r"(?:Current\s+)?(?:Loan\s+)?Mortgage\s+Servicer[^:\n]*:\s*(.+?)" + _STOP, re.I | re.S)
_DEED_REF = re.compile(
    r"Reference\s+Number\s+(?:of\s+(?:the\s+)?)?Deed\s+of\s+Trust\s*:\s*(?:Instrument\s+No\.?\s*)?([\w\-]+)",
    re.I,
)
_PARCEL = re.compile(r"Parcel\s+Number\(?s?\)?\s*:\s*([\w\-]+)", re.I)

# ── Auction: "will on 7/10/2026, at 10:00 A.M. <location> sell at public auction"
# Accept AM / A.M. / a.m. (dotted, Codex P1) and a multi-line location (.+? with re.S).
# The location INTRODUCER varies by source/trustee — Tacoma/some Snoho say "at <loc>",
# other Snoho notices say "On the Steps in Front of <loc>" / "Outside <loc>". So we do
# NOT require "at" before the location (that dropped auction_date on 5/7 Snoho notices);
# we capture everything up to "sell at public auction" and strip a leading "at " connector
# in parse_nts_notice. The lazy (.+?) is safe because callers parse ONE notice block at a
# time (PDF crawler splits first) — it can't drift across a notice boundary (Codex consult).
_AUCTION = re.compile(
    r"will\s+on\s+(\d{1,2}/\d{1,2}/\d{4})\s*,?\s*at\s+(\d{1,2}:\d{2}\s*[AP]\.?M\.?)\s+"
    r"(.+?)\s+sell\s+at\s+public\s+auction",
    re.I | re.S,
)
# ── Property: "[More] commonly known as: <addr> [Subject to|which is subject…]".
# Stop at "Subject to" (Quality Loan) OR "which is subject" (North Star) OR section
# II, same line or next (a one-line layout would otherwise swallow the deed body).
_COMMONLY_KNOWN = re.compile(
    r"commonly\s+known\s+as\s*:\s*(.+?)"
    r"(?=\s+(?:which\s+is\s+)?[Ss]ubject\s+to\b|\s+II\.\s|\n\n|\Z)",
    re.I | re.S,
)
# ── Section IV "sum owing on the obligation": two real phrasings —
#   North Star : "...is: Principal $185,895.06"
#   Quality Loan: "...is: The principal sum of $423,798.66"
# So after "principal" we lazily skip non-$ chars ("sum of ") to the first dollar
# figure. [^$] can't cross a '$', so the capture is pinned to the amount that
# immediately follows "principal" — never a later figure (interest, fees).
_PRINCIPAL_OWING = re.compile(
    r"sum\s+owing\s+on\s+the\s+obligation[^$]*?principal[^$]*?\$([\d,]+\.\d{2})",
    re.I | re.S,
)
# ── "Note Amount: $234,533.00" (original loan size)
_NOTE_AMOUNT = re.compile(r"Note\s+Amount\s*:?\s*\$?([\d,]+\.\d{2})", re.I)
# ── NOD transmittal date ("by both first class and certified mail on 1/20/2026")
_NOD_DATE = re.compile(r"certified\s+mail\s+on\s+(\d{1,2}/\d{1,2}/\d{4})", re.I)

# ── King (Queen Anne & Magnolia News) auction layouts use MONTH-NAME dates, which
# the numeric _AUCTION above misses. Two phrasings seen live:
#   Affinia: "...will on July 24, 2026, at 9:00 AM sell at public auction located <loc>..."
#   MTC:     "...will sell at public auction ... on July 24, 2026, 09:00 AM, <loc>..."
# Capture DATE + TIME (the load-bearing is_valid_nts fields) via the shared
# "on <month date>[,] [at] <time>" shape. Requiring the trailing TIME keeps this from
# matching the deed's recording/mailing dates (those carry no time). Tried ONLY when
# the numeric _AUCTION misses, so Tacoma/Snohomish parsing is unchanged.
_MONTHS = ("January|February|March|April|May|June|July|August|September|October|"
           "November|December")
_MONTH_DATE = rf"(?:{_MONTHS})\.?\s+\d{{1,2}},?\s+\d{{4}}"
_MONTH_NUM = {m.lower(): i for i, m in enumerate(_MONTHS.split("|"), start=1)}
_AUCTION_MONTHNAME = re.compile(
    rf"\bon\s+({_MONTH_DATE})\s*,?\s*(?:at\s+)?(\d{{1,2}}:\d{{2}}\s*[AP]\.?M\.?)", re.I)
# Best-effort location for the two King layouts (NOT required for validity/matching).
_AUCTION_LOC_LOCATED = re.compile(
    r"sell\s+at\s+public\s+auction\s+located\s+(.+?)\s+to\s+the\s+highest", re.I | re.S)
_AUCTION_LOC_AFTERTIME = re.compile(
    rf"\bon\s+{_MONTH_DATE}\s*,?\s*(?:at\s+)?\d{{1,2}}:\d{{2}}\s*[AP]\.?M\.?\s*,\s*"
    r"(.+?)(?=\s*,?\s*to\s+the\s+highest|\.\s|$)", re.I | re.S)


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
    whitespace; _COMMONLY_KNOWN already starts the capture after the label.

    'WASHINGTON' is normalized to 'WA' ONLY in the STATE position (before a ZIP or
    at end) — NOT inside a street name like 'WASHINGTON BLVD' (Codex: a global
    rewrite corrupted the match key, since the lead side never abbreviates streets).
    """
    if not raw:
        return None
    addr = " ".join(raw.split()).strip().rstrip(".,")
    addr = re.sub(r"\bWASHINGTON\b(?=\s*,?\s*\d{5}(?:-\d{4})?\s*$|\s*$)", "WA", addr, flags=re.I)
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
        loc = " ".join(am.group(3).split()).strip().rstrip(".,")
        # Drop the redundant grammatical "at" connector ("at <time> at <loc>") so the
        # location reads cleanly; keep descriptive introducers like "On the Steps…".
        loc = re.sub(r"^at\s+", "", loc, flags=re.I)
        # Drift guard (Codex): a real sale location never spans into the next notice or
        # runs hundreds of chars. If it does, the match is suspect — discard it whole.
        if "NOTICE OF TRUSTEE" not in loc.upper() and len(loc) <= 300:
            auction_date = am.group(1).strip()
            auction_time = " ".join(am.group(2).split())
            auction_location = loc or None

    # King fallback (month-name dates) — ONLY when the numeric _AUCTION missed, so
    # Tacoma/Snohomish behavior is unchanged. Date+time are load-bearing; location
    # is best-effort (one of the two King layouts).
    if auction_date is None:
        km = _AUCTION_MONTHNAME.search(text)
        if km:
            auction_date = " ".join(km.group(1).split()).strip().rstrip(",")
            auction_time = " ".join(km.group(2).split())
            lm = _AUCTION_LOC_LOCATED.search(text) or _AUCTION_LOC_AFTERTIME.search(text)
            if lm:
                loc = " ".join(lm.group(1).split()).strip().rstrip(".,")
                if "NOTICE OF TRUSTEE" not in loc.upper() and 0 < len(loc) <= 300:
                    auction_location = loc

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
    """Parse an auction date string to a date; None if unparseable.

    Accepts M/D/YYYY (Tacoma/Snohomish) OR "Month D, YYYY" (King papers).
    """
    if not mdy:
        return None
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\b", mdy)
    if m:
        mm, dd, yyyy = (int(g) for g in m.groups())
        try:
            return date(yyyy, mm, dd)
        except ValueError:
            return None
    mn = re.match(rf"\s*({_MONTHS})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})", mdy, re.I)
    if mn:
        try:
            return date(int(mn.group(3)), _MONTH_NUM[mn.group(1).lower()], int(mn.group(2)))
        except (ValueError, KeyError):
            return None
    return None


def notice_to_row(
    parsed: dict[str, Any],
    source_url: str,
    today: date,
    *,
    source: str = SOURCE,
    county: str = COUNTY,
) -> dict[str, Any] | None:
    """Transform a parsed notice into an nts_notices upsert row, or None if unusable.

    Adds the normalized match key (address_intel.address_match_key — the SAME key
    the matcher computes for a lead), an is_active flag (False once the auction is
    in the past), and a content hash for dedup / source-drift detection. Returns
    None for non-NTS pages (is_valid_nts) so the caller skips them.

    `source` / `county` default to the Tacoma Daily Index (Pierce) so existing
    callers are unchanged, but the Pacific Publishing PDF crawlers MUST pass their
    own (e.g. source='pacific_publishing_snohomish_tribune', county='snohomish').
    The matcher scopes notices by county, so a wrong county silently routes a notice
    to the wrong leads — these are the cache row's tenant-of-record fields (Codex).
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
        "source": source,
        "ts_number": parsed["ts_number"],
        "county": county,
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
