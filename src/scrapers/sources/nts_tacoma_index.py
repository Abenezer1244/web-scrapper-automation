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
# ANY dated legal-notice article link on a listing page (NTS or not). Used only to
# tell "this listing page still has entries" from "we've paginated past the last page"
# — the mixed feed interleaves probate/bids/name-changes/trustee-sales, so a page with
# zero NTS is NOT the end of the listing (Bug A: the old crawler treated it as the end).
_ANY_DATED_ARTICLE = re.compile(
    r'href="https?://(?:www\.)?tacomadailyindex\.com/\d{4}/\d{2}/\d{2}/[^"]+"',
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
    # A second common Pierce layout puts "Grantee(s): <trustee> ... Original
    # beneficiary of the deed of trust: <lender>" right after the grantor NAME on the
    # same line. Those labels weren't in _STOP, so the non-greedy grantor value ran
    # past the owner into the trustee/beneficiary text. Colon-anchored so an odd
    # entity/trust name containing the word can't truncate a real grantor (Codex).
    r"Grantee\(?s?\)?\s*:|Original\s+beneficiary(?:\s+of\s+the\s+deed\s+of\s+trust)?\s*:|"
    # The Clark / MTC dual-label layout prints "Current Beneficiary … of the Deed of
    # Trust: <lender> Original Trustee of the Deed of Trust: <original>" — without an
    # "Original Trustee" stop the beneficiary value bled straight into the original-
    # trustee text (live 2026-07-04). Added alongside the existing "Original beneficiary"
    # stop; a value stopping EARLIER at a real label is always safe (Codex).
    r"Current\s+Beneficiary|Current\s+Trustee|Original\s+Trustee|Current\s+(?:Loan\s+)?Mortgage|"
    # "Subject to" is a stop for the address-style values, but NOT when it opens a
    # parenthetical title note inside a NAME: "BARBARA J. HILL, AS SURVIVING SPOUSE
    # ( SUBJECT TO SCH. B, 4 A ) Current Beneficiary …" (live 2026-09-02) was cut to
    # "… SURVIVING SPOUSE (" and shipped as the lead's party_name. The fixed-width
    # lookbehinds skip a "Subject to" preceded by "(" and up to three whitespace
    # chars (space / newline / tab) so the value runs on to the next real label;
    # strip_vesting_clause drops the note at read time.
    r"which\s+is\s+subject|(?<!\()(?<!\(\s)(?<!\(\s\s)(?<!\(\s\s\s)Subject\s+to\b|"
    r"I\.\s*NOTICE|II\.)|\n|\Z)"
)

# TS#: label variants (TS #, T.S. No., Trustee Sale No./Number) + value formats that
# vary by trustee (North Star YY-NNNNN, Quality Loan WA-25-…-RM). Stop before a
# trailing "-NOTICE…" (title-line concat) and at whitespace/EOL (Codex P1).
_TS_NUMBER = re.compile(
    # Label boundaries (live 2026-09-02): "defaul[ts no]w in arrears" produced
    # ts_number "w". The label may not be glued to a preceding letter, and "No"
    # may not run straight into another letter ("now"). "No." / "No:" / "No 123" /
    # "T.S.#" all still match.
    r"(?<![A-Za-z])(?:T\.?S\.?\s*#|T\.?S\.?\s*No\.?(?![A-Za-z])|Trustee\s+Sale\s+(?:No\.?|Number))\s*:?\s*"
    r"([A-Za-z0-9][A-Za-z0-9\-]*?)(?=\s|$|-?NOTICE\b)",
    re.I,
)
_TITLE_ORDER = re.compile(r"Title\s*Order\s*(?:#|No\.?)\s*:\s*([\w\-]+)", re.I)
# Grantor / Grantor(s) [optional "for Recording Purposes …"] : value
_GRANTOR = re.compile(r"Grantor\(?s?\)?[^:\n]*:\s*(.+?)" + _STOP, re.I | re.S)
_BENEFICIARY = re.compile(r"(?:Current\s+)?Beneficiary[^:\n]*:\s*(.+?)" + _STOP, re.I | re.S)
# The ACTING trustee is the CURRENT trustee, not the original. Some layouts (Clark /
# MTC) print BOTH "Original Trustee of the Deed of Trust: <x>" and "Current Trustee of
# the Deed of Trust: <y>"; a single "(?:Current\s+)?Trustee…" regex matches the FIRST
# occurrence = the ORIGINAL trustee (live 2026-07-04: captured the original title
# company instead of the acting trustee conducting the sale). So prefer an explicit
# Current label; else the first bare "Trustee of the Deed of Trust:" NOT preceded by
# "Original" (see _extract_trustee). Require the precise "Trustee of the Deed of Trust:"
# label so neither "Trustee Sale No.:" nor a prose "the undersigned Trustee," is read as
# the trustee NAME (Codex P2 + the one-line-layout 09:00-time-colon trap). Single-label
# Pierce/Snohomish notices resolve via the bare path, unchanged from the prior regex.
_TRUSTEE_CURRENT = re.compile(
    r"Current\s+Trustee\s+of\s+the\s+Deed\s+of\s+Trust\s*:\s*(.+?)" + _STOP,
    re.I | re.S,
)
_TRUSTEE_ANY = re.compile(
    r"Trustee\s+of\s+the\s+Deed\s+of\s+Trust\s*:\s*(.+?)" + _STOP,
    re.I | re.S,
)
_ORIGINAL_BEFORE = re.compile(r"Original\s*$", re.I)
_SERVICER = re.compile(r"(?:Current\s+)?(?:Loan\s+)?Mortgage\s+Servicer[^:\n]*:\s*(.+?)" + _STOP, re.I | re.S)
_DEED_REF = re.compile(
    r"Reference\s+Number\s+(?:of\s+(?:the\s+)?)?Deed\s+of\s+Trust\s*:\s*(?:Instrument\s+No\.?\s*)?([\w\-]+)",
    re.I,
)
# Second live layout (private/law-firm trustee, 2026-09-02): "Reference Numbers of
# Documents Referenced: Instrument Number 202212290190 (Deed of Trust) 202605120147
# (Appointment of Successor Trustee)". Capture ONLY the instrument explicitly tagged
# "(Deed of Trust)" — never an adjacent appointment/assignment number (Codex).
_DEED_REF_TAGGED = re.compile(r"(\d{8,14})\s*\(\s*Deed\s+of\s+Trust\s*\)", re.I)
# Parcel label variants: "Parcel Number(s):" (North Star / Quality Loan / Affinia),
# plus "[Assessor's] Tax Parcel No(s).:" (Clear Recon / older law-firm notices — live
# 2026-06-26). A multi-parcel notice lists several after the colon; we capture the
# FIRST (a valid match key — the matcher keys on any one exact parcel).
_PARCEL = re.compile(
    r"(?:Parcel\s+Number\(?s?\)?|(?:Assessor'?s\s+)?Tax\s+Parcel\s+(?:No|Number)s?\.?"
    # "Assessor's Parcel No. 766500-0020" (law-firm layout, live 2026-09-02) — no
    # colon, no "Tax". Bounded to the label so prose can't feed it.
    r"|Assessor'?s\s+Parcel\s+(?:No|Number)s?\.?)"
    r"\s*:?\s*([\w\-]+)",
    re.I,
)

# ── Auction: "will on 7/10/2026, at 10:00 A.M. <location> sell at public auction"
# Accept AM / A.M. / a.m. (dotted, Codex P1) and a multi-line location (.+? with re.S).
# The location INTRODUCER varies by source/trustee — Tacoma/some Snoho say "at <loc>",
# other Snoho notices say "On the Steps in Front of <loc>" / "Outside <loc>". So we do
# NOT require "at" before the location (that dropped auction_date on 5/7 Snoho notices);
# we capture everything up to "sell at public auction" and strip a leading "at " connector
# in parse_nts_notice. The lazy (.+?) is safe because callers parse ONE notice block at a
# time (PDF crawler splits first) — it can't drift across a notice boundary (Codex consult).
# Live 2026-09-02 variants folded in (each still anchored to "sell at public auction"):
# an explicit weekday ("will, on Friday, August 28, 2026"), "at the hour of 10:00 AM"
# between date and time, and a comma after the time. Weekdays are spelled out — never
# a bare \w+day — so unrelated text can't be swallowed (Codex).
_WEEKDAY = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
_HOUR_OF = r"(?:the\s+hour\s+of\s+)?"
_AUCTION = re.compile(
    rf"will\s*,?\s*on\s+(?:{_WEEKDAY})?(\d{{1,2}}/\d{{1,2}}/\d{{4}})\s*,?\s*at\s+{_HOUR_OF}"
    r"(\d{1,2}:\d{2}\s*[AP]\.?M\.?)\s*,?\s*"
    r"(.+?)\s+sell\s+at\s+public\s+auction",
    re.I | re.S,
)
# ── Property: "[More] commonly known as[:] <addr> [Subject to|which is subject…]".
# Stop at "Subject to" (Quality Loan) OR "which is subject" (North Star) OR
# "The above property is …" (Affinia — without this the boilerplate words before
# "subject" leak into the address and poison the normalized match key) OR section
# II, same line or next (a one-line layout would otherwise swallow the deed body).
# The colon is optional ONLY behind "More commonly known as" (MTC notices say
# "More commonly known as 1814 FRANKLIN AVE E…" with no colon — live 2026-07-01;
# requiring it silently dropped the address). Bare "commonly known as" KEEPS the
# colon requirement: prose like "Federal National Mortgage Association commonly
# known as Fannie Mae" would otherwise hijack the capture and poison the match key
# (Codex High, 2026-07-01 review).
_COMMONLY_KNOWN = re.compile(
    r"(?:More\s+commonly\s+known\s+as\s*:?|commonly\s+known\s+as\s*:)\s*(.+?)"
    r"(?=\s+The\s+above\s+property\b|\s+(?:which\s+is\s+)?[Ss]ubject\s+to\b"
    # Stop before a trailing parcel line (Codex P2): some layouts print "Commonly
    # known as: <addr> Tax Parcel Nos.: <apn> which is subject to…" — without this the
    # parcel text leaks INTO property_address and poisons the normalized match key, so
    # a lead at that address won't attach by address.
    r"|\s+(?:Assessor'?s\s+)?Tax\s+Parcel\b|\s+Assessor'?s\s+Parcel\b|\s+Parcel\s+Number\b"
    r"|\s+II\.\s|\n\n|\Z)",
    re.I | re.S,
)
# ── Section IV "sum owing on the obligation" — real phrasings:
#   North Star   : "...secured by the Deed of Trust is: Principal $185,895.06, together…"
#   Quality Loan : "...secured by the Deed of Trust is: The principal sum of $423,798.66,…"
#   Quality Loan, matured/commercial loan (live 2026-09-02, TS# WA-26-1050840-BB):
#                  "...sum owing on the MATURED obligation secured by the Deed of Trust
#                   is: $575,150.38. V. …" — no "principal" word at all.
# The old single regex required the literal "on the obligation" AND a following
# "principal", so a matured/balloon notice silently lost its amount and a real lead
# shipped with a blank Default Owed. Now: anchor on "sum owing on the [qualifier]
# obligation(s)" only — as tolerant of the connecting legal text ("… as evidenced by
# the Note and secured by the Deed of Trust is:") as the old regex was (Codex) —
# bound the search to section IV (up to the next "V." roman marker or a hard char
# cap), PREFER the figure labelled "principal", and only when section IV carries no
# principal label take the FIRST dollar figure within a short window of the anchor.
# The bound + the window keep the capture pinned inside section IV, so a section-V/VI
# figure can never be mistaken for the sum owing.
# Qualifiers are any 1-3 non-space tokens ("matured", "matured/commercial",
# "matured-loan") so punctuation in a trustee's wording can't defeat the anchor.
_SUM_OWING_ANCHOR = re.compile(r"sum\s+owing\s+on\s+the\s+(?:\S+\s+){0,3}obligations?\b", re.I)
_SECTION_IV_SPAN_CHARS = 600
# Section V marker: "V." followed by whitespace or the next word ("V. The", "V.The",
# "V .\nThe"); \b keeps "IV." from matching.
_SECTION_IV_END = re.compile(r"\bV\s?\.(?=\s|[A-Z])")
_PRINCIPAL_LABELED = re.compile(r"principal[^$]{0,40}?\$([\d,]+\.\d{2})", re.I | re.S)
# "… secured by the Deed of Trust is: $575,150.38" sits ~45 chars past the anchor.
_FIRST_DOLLAR_NEAR_ANCHOR = re.compile(r"^[^$]{0,120}?\$([\d,]+\.\d{2})", re.S)


def _principal_owing(text: str) -> Decimal | None:
    """Section IV amount owing on the obligation (see the phrasings above); None if
    the statutory sentence is absent or carries no dollar figure inside section IV."""
    m = _SUM_OWING_ANCHOR.search(text)
    if not m:
        return None
    span = text[m.end(): m.end() + _SECTION_IV_SPAN_CHARS]
    end = _SECTION_IV_END.search(span)
    if end:
        span = span[: end.start()]
    hit = _PRINCIPAL_LABELED.search(span) or _FIRST_DOLLAR_NEAR_ANCHOR.search(span)
    if not hit:
        return None
    try:
        return Decimal(hit.group(1).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
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
_TIME = r"\d{1,2}:\d{2}\s*[AP]\.?M\.?"
# Both King layouts put the sale DATE + TIME just BEFORE "sell at public auction"
# (Affinia adjacent: "will on <date>, at <time> sell…"; MTC ~200 chars before:
# "on <date>, <time>, <loc> … the undersigned Trustee, will sell…"). Requiring the
# auction verb WITHIN 600 chars after the time ANCHORS the match to the sale so a
# stray dated timestamp elsewhere in the notice can't be read as the auction (Codex).
# group(1)=date, group(2)=time. The matched span (group 0) also carries the location.
_AUCTION_KING = re.compile(
    rf"\bon\s+(?:{_WEEKDAY})?({_MONTH_DATE})\s*,?\s*(?:at\s+)?{_HOUR_OF}({_TIME})"
    r"[\s\S]{0,600}?sell\s+at\s+public\s+auction", re.I)
# Best-effort location within the matched span: Affinia "…located <loc> to the highest";
# MTC "<time>, <loc>…". Not required for validity/matching.
_AUCTION_KING_LOC_A = re.compile(r"located\s+(.+?)\s+to\s+the\s+highest", re.I | re.S)
_AUCTION_KING_LOC_B = re.compile(
    rf"{_TIME}\s*,\s*(.+?)(?=,\s*to\s+the\s+highest|the\s+undersigned|will\s+sell|$)",
    re.I | re.S)

# ── Affinia with a NUMERIC date (live 2026-09-03, Queen Anne & Magnolia News / King).
# Affinia puts the location AFTER the verb ("…will on 08/14/2026, at 10:00 AM sell at
# public auction located at the 4th Avenue Entrance…"). _AUCTION requires a NON-EMPTY
# location BETWEEN the time and the verb, so it cannot match this; _AUCTION_KING allows
# the location after but only accepts a MONTH-NAME date; _AUCTION_WORDED needs "Nth day
# of <Month> … o'clock". So all three missed and the notice was DISCARDED whole
# (is_valid_nts needs auction_date) — 3 of 5 notices on the 08-05-26 issue and 2 of 2
# on the 09-02-26 issue, i.e. every Affinia sale in King.
#
# Deliberately the TIGHTEST of the four: the verb must follow the time IMMEDIATELY
# (only optional whitespace/comma), and the whole thing must hang off "Trustee will"
# (Codex). A numeric date is common inside these notices (deed recording, "Interest
# Paid To", publication dates), so — unlike the month-name _AUCTION_KING — this one
# gets NO 600-char window to reach the verb: a recording date is never immediately
# followed by "sell at public auction". Groups (1)=date, (2)=time match _AUCTION_KING's
# so the caller handles both identically. Fully anchored literals, no nested
# quantifiers, no unbounded '.' -> linear, no backtracking blowup on a 45k block.
_AUCTION_NUM_LOC_AFTER = re.compile(
    # "the undersigned Trustee, will on …" is a real appositive in these papers (2 of
    # 5 notices in the 08-05-26 issue), so the comma after Trustee is optional (Codex).
    rf"Trustee\s*,?\s+will\s*,?\s*on\s+(?:{_WEEKDAY})?(\d{{1,2}}/\d{{1,2}}/\d{{4}})\s*,?\s*"
    rf"at\s+{_HOUR_OF}({_TIME})\s*,?\s*sell\s+at\s+public\s+auction",
    re.I,
)

# ── Postponement override (live 2026-09-03, King / MTC). A re-published notice keeps
# its ORIGINAL sale sentence and marks the new date INLINE:
#   "…GIVEN that on June 26, 2026, 09:00 AM***THE SALE WAS POSTPONED TO 09/18/2026 @
#    9:00AM***, Main Entrance, King County Administration Building…"
# Every auction pattern reads the sentence's own date, so the notice lands with a PAST
# auction date, is_active flips False, and a sale that is still upcoming vanishes from
# the product. That is exactly what buried a real King sale (TS WA05000073-24-2, truly
# 09/18/2026) and left the lead unreachable. Applies to ALL sources — postponements are
# not county-specific.
# Only ever moves the date FORWARD: a postponement cannot go backwards, so an unrelated
# date elsewhere in the notice can never pull a sale earlier than its stated one.
_POSTPONED = re.compile(
    r"(?:POSTPONED|CONTINUED|RESCHEDULED)\s+TO\s*:?\s*"
    rf"({_MONTH_DATE}|\d{{1,2}}/\d{{1,2}}/\d{{4}})"
    rf"(?:\s*(?:@|at)?\s*({_TIME}))?",
    re.I,
)

# ── Ordinal worded auction date (Clear Recon / older law-firm Tacoma notices, live
# 2026-06-26): "...Trustee will on the 17th day of July, 2026, at the hour of 10:00
# o'clock AM <loc> ... sell at public auction". Neither the numeric _AUCTION nor the
# King month-name _AUCTION_KING matches this shape, so these real Pierce sales were
# dropped (auction_date None). Anchored to "sell at public auction" within 600 chars
# (like _AUCTION_KING) so a stray dated line can't be read as the sale. Tried ONLY
# when _AUCTION missed (am is None), so numeric-format parsing is byte-identical.
# group(1)=day, (2)=month, (3)=year, (4)=HH:MM, (5)=A|P. Normalized to "Month D, YYYY"
# so it reuses the existing _to_date month-name path.
_AUCTION_WORDED = re.compile(
    rf"will\s+on\s+the\s+(\d{{1,2}})(?:st|nd|rd|th)\s+day\s+of\s+({_MONTHS})\.?,?\s+(\d{{4}})"
    # "10 o'clock A.M." (no minutes) is a real layout (live 2026-09-02) -> minutes optional.
    r"\s*,?\s*at\s+(?:the\s+hour\s+of\s+)?(\d{1,2}(?::\d{2})?)\s*o'?clock\s*([AP])\.?\s*M\.?"
    r"[\s\S]{0,600}?sell\s+at\s+public\s+auction",
    re.I,
)


def _first(pattern: re.Pattern, text: str) -> str | None:
    m = pattern.search(text)
    if not m:
        return None
    val = " ".join(m.group(1).split()).strip().rstrip(".,")
    return val or None


def _clean_value(raw: str) -> str | None:
    """Collapse whitespace + strip trailing punctuation (matches _first's cleaning)."""
    val = " ".join(raw.split()).strip().rstrip(".,")
    return val or None


def _extract_trustee(text: str) -> str | None:
    """The CURRENT (acting) trustee of the deed of trust.

    Prefer an explicit "Current Trustee of the Deed of Trust:" label; else the first
    bare "Trustee of the Deed of Trust:" that is NOT the "Original Trustee…" (the
    dual-label Clark/MTC layout carries both). Single-label Pierce/Snohomish notices
    resolve via the bare path — byte-identical to the prior single-regex behavior.
    Returns None when only an original trustee is named (an original title company is
    not the acting trustee; None is more honest than a misleading name — Codex).
    """
    m = _TRUSTEE_CURRENT.search(text)
    if m:
        return _clean_value(m.group(1))
    for m in _TRUSTEE_ANY.finditer(text):
        if _ORIGINAL_BEFORE.search(text[max(0, m.start() - 12):m.start()]):
            continue  # this is the "Original Trustee…" label — skip to the acting one
        return _clean_value(m.group(1))
    return None


def _money(pattern: re.Pattern, text: str) -> Decimal | None:
    m = pattern.search(text)
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _truncate(value: str | None, limit: int) -> str | None:
    """Bound a display-only field to its nts_notices column width (None-safe).

    Never use this on a match/identity field (parcel, ts_number) — a truncated key
    can silently collide with or false-match a real value; those are nulled/skipped
    in notice_to_row instead.
    """
    if value is None:
        return None
    return value[:limit].rstrip() or None


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

    # King fallback (month-name dates) — ONLY when the numeric _AUCTION did not match
    # at all (am is None), so Tacoma/Snohomish behavior is byte-identical even for a
    # numeric notice whose location drift-guard rejected it (Codex). Date+time are
    # load-bearing; location (same anchored match) is best-effort.
    if am is None:
        # Month-name layouts first, then the numeric location-after layout (Affinia).
        # Both expose group(1)=date, group(2)=time, so one branch serves both; _to_date
        # already accepts M/D/YYYY and "Month D, YYYY".
        km = _AUCTION_KING.search(text) or _AUCTION_NUM_LOC_AFTER.search(text)
        if km:
            auction_date = " ".join(km.group(1).split()).strip().rstrip(",")
            auction_time = " ".join(km.group(2).split())
            # Location from a BOUNDED window around the anchored match (no whole-notice
            # drift, Codex P3) — extend ~200 chars past the verb to reach Affinia's
            # "…sell at public auction located <loc> to the highest". Best-effort.
            span = text[km.start():km.end() + 200]
            lm = _AUCTION_KING_LOC_A.search(span) or _AUCTION_KING_LOC_B.search(span)
            if lm:
                loc = " ".join(lm.group(1).split()).strip().rstrip(".,")
                # For the location-AFTER layout the match ends at the verb, so the
                # search window starts before it — reject a capture that swallowed the
                # verb instead of the venue (Codex). LOC_A normally anchors past it.
                if ("NOTICE OF TRUSTEE" not in loc.upper()
                        and "SELL AT PUBLIC AUCTION" not in loc.upper()
                        and 0 < len(loc) <= 300):
                    auction_location = loc
        else:
            # Ordinal worded date fallback ("17th day of July, 2026" — Clear Recon /
            # older law-firm notices). Normalize to "Month D, YYYY" for _to_date;
            # time to "HH:MM AM". Location left None (these notices vary; not required).
            wm = _AUCTION_WORDED.search(text)
            if wm:
                day, month, year, hhmm, ampm = wm.groups()
                auction_date = f"{month.title()} {int(day)}, {year}"
                if ":" not in hhmm:
                    hhmm = f"{hhmm}:00"
                auction_time = f"{hhmm} {ampm.upper()}M"

    # An inline "POSTPONED TO <date>" supersedes whatever the sale sentence says — the
    # original date is stale the moment it is printed. Forward-only (see _POSTPONED), so
    # this can never pull a sale earlier or resurrect a genuinely finished auction.
    if auction_date:
        pm = _POSTPONED.search(text)
        if pm:
            original, moved = _to_date(auction_date), _to_date(pm.group(1))
            if moved and (original is None or moved >= original):
                auction_date = " ".join(pm.group(1).split()).strip().rstrip(",")
                if pm.group(2):
                    auction_time = " ".join(pm.group(2).split())

    return {
        # A trustee's page TITLE can carry a trailing dash ("TS# WA-26-1035144-SW-
        # NOTICE OF…", live 2026-09-02) while the body says "WA-26-1035144-SW"; the
        # title matches first. Trim trailing hyphens only — the TS# is the natural key.
        "ts_number": (_first(_TS_NUMBER, text) or "").rstrip("-") or None,
        "title_order": _first(_TITLE_ORDER, text),
        "grantor": _first(_GRANTOR, text),
        "beneficiary": _first(_BENEFICIARY, text),
        "trustee": _extract_trustee(text),
        "servicer": _first(_SERVICER, text),
        "deed_reference": _first(_DEED_REF, text) or _first(_DEED_REF_TAGGED, text),
        "parcel": _first(_PARCEL, text),
        "auction_date": auction_date,
        "auction_time": auction_time,
        "auction_location": auction_location,
        "property_address": _clean_address(_first(_COMMONLY_KNOWN, text)),
        # Section IV "sum owing ... Principal" is the headline default/payoff figure.
        "principal_owing": _principal_owing(text),
        "note_amount": _money(_NOTE_AMOUNT, text),
        "nod_date": _first(_NOD_DATE, text),
    }


def parse_tacoma_notice(text: str) -> dict[str, Any]:
    """parse_nts_notice + a SURROGATE ts_number for notices that carry no trustee TS#.

    Drop-in for the crawler (parallels nts_king_pdf.parse_king_notice). Several real
    Pierce trustees (Clear Recon, "In re …" law-firm notices — live 2026-06-26) print
    NO "Trustee Sale No.", so parse_nts_notice returns ts_number=None and is_valid_nts
    drops the notice — losing a real, actionable sale (e.g. a $661k Clear Recon sale).

    The nts_notices natural key is (source, ts_number), so a storable notice needs one.
    REF-<deed recording #> is unique per deed of trust (an amended NTS for the same loan
    collapses to one row — matches the upsert's latest-wins). APN-<parcel> is a DEGRADED
    fallback (repeat foreclosures on one parcel over time collapse into a row); used only
    when no deed reference exists. This is a CACHE dedup key, NOT a real trustee-sale id;
    the matcher keys on parcel/address, never ts_number, so a surrogate is match-safe.
    """
    parsed = parse_nts_notice(text)
    if not parsed.get("ts_number"):
        ref = parsed.get("deed_reference")
        if ref:
            parsed["ts_number"] = f"REF-{ref}"[:64]
        elif parsed.get("parcel"):
            parsed["ts_number"] = f"APN-{parsed['parcel']}"[:64]
    return parsed


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


def listing_has_entries(listing_html: str) -> bool:
    """True if the listing page carries ANY dated legal-notice article link.

    Distinguishes a normal page (probate/bids/NTS interleaved) from a page past the
    end of the listing (empty). The crawler keeps paginating while this is True and a
    page yields no NTS — a zero-NTS page mid-listing is expected, NOT the end.
    """
    return bool(_ANY_DATED_ARTICLE.search(listing_html))


def collect_notice_urls(fetch_page, *, max_pages: int, max_notices: int) -> list[str]:
    """Walk the mixed legal-notices listing, collecting NTS URLs across ALL pages.

    `fetch_page(page:int)` returns either ``(status_code, html)`` or ``None`` (fetch
    failed — a transient failure on one page must not abandon later pages). Pure: the
    caller injects I/O, so the pagination logic is unit-tested with a real in-memory
    fetcher (no mocks).

    Bug A fix: the previous crawler stopped at the FIRST page with no NTS notice. But
    the feed interleaves notice types in reverse-chron order, so page 1 is routinely
    all probate/bids while trustee sales sit on later pages. We now stop only on a
    genuine end signal:
      * a page that fetched non-200 (the CMS 404s beyond the last page), or
      * a page with zero dated article links (listing_has_entries False), or
      * the max_notices cap, or
      * max_pages exhausted.
    """
    urls: list[str] = []
    for page in range(1, max_pages + 1):
        result = fetch_page(page)
        if result is None:
            continue  # transient fetch failure — a later page may still succeed
        status, html = result
        if status != 200:
            break  # past the last listing page, or blocked — stop paginating
        for u in extract_notice_urls(html):
            if u not in urls:
                urls.append(u)
        if len(urls) >= max_notices:
            break
        if not listing_has_entries(html):
            break  # genuine end of the listing (empty page)
    return urls[:max_notices]


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
    # ts_number is IDENTITY (the (source, ts_number) upsert key), not display: an
    # overlong value is parser garbage, and truncating it could collide two distinct
    # notices under one key — treat it as missing and skip cleanly (Codex 2026-07-01).
    # Source-specific surrogate logic (King REF-/APN-) runs BEFORE this and already
    # bounds its keys to 64.
    if len(parsed["ts_number"]) > 64:
        return None
    from src.utils.address_intel import address_match_key

    auction = _to_date(parsed.get("auction_date"))
    # Mis-parse detector (live 2026-07-01): on a no-colon layout the colon field
    # regexes scan to the SAME first colon (e.g. inside "10:00 AM") and assign one
    # identical boilerplate blob to grantor + beneficiary (usually servicer too, but
    # grantor==beneficiary alone is already a structural parse failure — Codex
    # Medium: don't require the servicer to also be present/identical). Null the
    # poisoned enrichment fields but KEEP the notice (its ts_number/date/address/
    # parcel still match leads). Length/boilerplate guard so a legitimate short
    # coincidence survives.
    grantor = _truncate(parsed.get("grantor"), 512)
    beneficiary = _truncate(parsed.get("beneficiary"), 255)
    raw_grantor = parsed.get("grantor")
    if (
        raw_grantor is not None
        and raw_grantor == parsed.get("beneficiary")
        and (len(raw_grantor) >= 100 or "public auction" in raw_grantor.lower()
             or "notice is hereby given" in raw_grantor.lower())
    ):
        grantor = beneficiary = None
    # Clamp display-only fields to their nts_notices column widths so one runaway
    # capture can never abort the whole notice's INSERT again (a live varchar(512)
    # error lost a notice). parcel is a MATCH key — a truncated parcel could
    # false-match a real lead, so an overlong one becomes None instead.
    addr = _truncate(parsed.get("property_address"), 512)
    norm = _truncate(address_match_key(addr), 512)
    parcel = parsed.get("parcel")
    if parcel and len(parcel) > 64:
        parcel = None
    trustee = _truncate(parsed.get("trustee"), 255)
    # Hash the load-bearing STORED values (post-clamp/null — Codex Low: hashing the
    # raw parsed values made discarded garbage churn raw_hash even when the stored
    # row was unchanged) so a re-crawl of an unchanged notice is a no-op and a
    # source/parser change is observable.
    payload = "|".join(
        str(v or "")
        for v in (parsed["ts_number"], parsed.get("auction_date"), addr, trustee,
                  beneficiary, parsed.get("principal_owing"), parcel)
    )
    raw_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return {
        "source": source,
        "ts_number": parsed["ts_number"],
        "county": county,
        "state": STATE,
        "parcel": parcel,
        "property_address": addr,
        "property_address_normalized": norm,
        "auction_date": auction,
        "auction_time": _truncate(parsed.get("auction_time"), 16),
        "auction_location": _truncate(parsed.get("auction_location"), 512),
        "grantor": grantor,
        "trustee": trustee,
        "beneficiary": beneficiary,
        "principal_owing": parsed.get("principal_owing"),
        "note_amount": parsed.get("note_amount"),
        "nod_date": _truncate(parsed.get("nod_date"), 32),
        "source_url": _truncate(source_url, 512),
        "raw_hash": raw_hash,
        "is_active": bool(auction and auction >= today),
    }
