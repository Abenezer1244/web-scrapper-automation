"""Pacific Publishing legal-notice PDF → NTS notice blocks (Snohomish / King).

WA NTS notices (RCW 61.24.040) are also published in Pacific Publishing weekly
"Legals" PDFs — the Snohomish County Tribune (Snohomish County) and the Queen
Anne & Magnolia News (King County). Unlike the Tacoma Daily Index (one HTML page
per notice), ONE weekly PDF carries MANY notices in the SAME statutory Quality
Loan / North Star layout the Tacoma parser already handles.

This module is the PDF INGESTION ADAPTER: extract text → repair column-wrap
artifacts → split into individual notice blocks. The per-notice field parsing is
REUSED from ``nts_tacoma_index.parse_nts_notice`` (the shared statutory parser);
we fork only the ingestion path, never the field regexes (Codex consult: sharing
the field extraction is good, sharing the source module is how regressions creep
in). The crawler then runs each block through the shared parser + ``notice_to_row``.

Security (Codex): the crawler downloads via ``safe_download_to_file`` (SSRF-guarded,
https-only, size-capped, stream-to-disk). This module additionally rejects non-PDF
bytes (``%PDF-`` magic), encrypted PDFs, and caps pages — so a hostile or huge PDF
cannot exhaust the worker.

Pure functions, no network — unit-tested against a REAL saved PDF fixture (no mocks).
"""
from __future__ import annotations

import re
from io import BytesIO

_PDF_MAGIC = b"%PDF-"
_MAX_PAGES = 40  # a weekly legals section is a handful of pages; cap hostile inputs

# Split on the statutory header ONLY. Critical (Codex): the notice BODY contains
# Title-Case boilerplate ("…this amended Notice of Trustee Sale…", mediation/
# housing-counselor prose) that a loose, case-insensitive match would treat as a new
# notice — chopping each real notice into fragments. The TRUE header is ALL-CAPS WITH
# THE POSSESSIVE: "NOTICE OF TRUSTEE'S SALE" (apostrophe variants + S). The boilerplate
# is Title-Case "Notice of Trustee Sale" (no possessive 'S) and is excluded by both
# the case-sensitivity AND the required ['’ʼ�]S. The lookahead keeps the header with
# its block. (normalize_pdf_text has already mapped � → '.)
_NOTICE_SPLIT = re.compile(r"(?=NOTICE\s+OF\s+TRUSTEE['’ʼ�]S\s+SALE)")
_HAS_HEADER = re.compile(r"NOTICE\s+OF\s+TRUSTEE['’ʼ�]S\s+SALE")

# De-hyphenate column-wrap breaks. TWO cases, because a hyphen before a wrap is
# either a SOFT hyphen the layout inserted, or a HARD hyphen that's part of an
# identifier:
#  1. SOFT (letter-hyphen-newline-letter): drop the inserted hyphen + join, both
#     cases — 'Par-\ncel'→'Parcel', 'SER-\nVICE'→'SERVICE', 'NORTH-\nEAST'→'NORTHEAST'.
#  2. HARD (a DIGIT is involved — a wrapped TS#/parcel/zip like 'WA-25-\n1012820'):
#     the hyphen is REAL, only the newline is the wrap → drop the newline, KEEP the
#     hyphen so the identifier stays intact ('WA-25-1012820'). Codex caught that
#     simply protecting digits (dropping neither) left 'WA-25- 1012820', which the
#     TS# regex then TRUNCATED to 'WA-25-' at the space.
# Tradeoff (Codex): a genuinely hyphenated surname/placename split at a line break
# ('Smith-\nJones') is joined by rule 1 — accepted because it only ever causes a
# MISSED match (the lead side, from the county recorder, is unaffected), never a
# WRONG one (the matcher's safe-side rule).
#
# The Snohomish Tribune layout inserts a SPACE before the soft-wrap hyphen
# ('MI -\nCHAEL', 'PROP -\nERTY', 'SOLEIMANZA -\nDEH'), so rule 1 allows optional
# whitespace between the letter and the hyphen. Without it the hyphen fell through
# to rule 2 (digit-ID protection) and survived as a stray 'MI -CHAEL' in the name —
# corrupting the homeowner's name for skip-trace. Both sides must still be LETTERS,
# so a digit identifier wrap ('WA-25-\n1012820') is untouched here and handled by
# rule 2.
_DEHYPHEN_WORD = re.compile(r"([A-Za-z])[ \t]*-\n[ \t]*([A-Za-z])")
_DEHYPHEN_ID = re.compile(r"-\n[ \t]*(?=\w)")


def extract_pdf_text(data: bytes, *, max_pages: int = _MAX_PAGES) -> str:
    """Extract text from a real, text-based legals PDF.

    Raises ValueError on a non-PDF (no ``%PDF-`` magic), an encrypted PDF, or an
    empty/garbage input — the crawler treats that as a fetch failure, not a notice.
    """
    if not data or not data.startswith(_PDF_MAGIC):
        raise ValueError("not a PDF (missing %PDF- magic)")
    import itertools

    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    if reader.is_encrypted:
        raise ValueError("encrypted PDF not supported")
    # islice over the LAZY page sequence so a hostile/huge PDF only ever has its first
    # max_pages text-extracted — `list(reader.pages)[:max_pages]` would materialize +
    # extract every page first, defeating the cap (Codex). The 25 MB download cap is the
    # primary guard; this bounds the per-page extraction CPU.
    pages = itertools.islice(reader.pages, max_pages)
    return "\n".join(p.extract_text() or "" for p in pages)


def normalize_pdf_text(raw: str) -> str:
    """Repair PDF column-wrap artifacts so the shared statutory regexes match.

    De-hyphenate word wraps (never identifiers), then collapse the layout newlines
    to single spaces, then squeeze runs of spaces. Apostrophe variants are mapped
    to a straight quote (the shared parser also does this, belt-and-suspenders).
    Order matters: de-hyphenation runs while the newlines are still present.
    """
    t = raw.replace("’", "'").replace("‘", "'").replace("�", "'")
    t = _DEHYPHEN_WORD.sub(r"\1\2", t)   # soft word-wrap: drop the inserted hyphen
    t = _DEHYPHEN_ID.sub("-", t)          # identifier wrap: keep the hyphen, drop the newline
    t = re.sub(r"\s*\n\s*", " ", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


# ── Pre-header identity preamble ──────────────────────────────────────────────
# Some trustees print the notice's OWN identity ("TS No <x> TO No <y>" for MTC /
# Trustee Corps, "TS #: <x> Title Order #: <y>" for North Star) IMMEDIATELY BEFORE
# the statutory header instead of inside the body. A header-only split orphans that
# preamble at the TAIL of the PREVIOUS block, so `_TS_NUMBER` — which searches the
# whole block and keeps the first match — gave notice N the TS number of notice N+1,
# and left the LAST notice with none at all (is_valid_nts requires one, so it was
# silently DROPPED). Verified live 2026-09-03 against the Snohomish Tribune
# "Legals - 8-5-26" PDF: 2 of the 6 delivered "Test 4" leads carried the following
# notice's TS number, and an 8th notice never became a row.
#
# The repair is deliberately NARROW. A "move the last TS label in the tail" rule
# would be WRONG (Codex): the Quality Loan layout repeats the notice's OWN TS number
# in its trailer ("Trustee Sale Number: WA-22-945105-SW Sale Line: ... IDSPub #..."),
# and moving that would recreate the same bug in reverse. So we move a run only when
# it (a) sits IMMEDIATELY adjacent to the next header with no other text between, and
# (b) leaves the block a TS number of its own. When either test fails we keep today's
# behaviour exactly — an uncertain block is never rewritten.
_PREHEADER_ITEM = (
    r"(?:T\.?\s*S\.?\s*(?:#|No\.?)|Trustee\s+Sale\s+(?:No\.?|Number)|"
    r"T\.?O\.?\s+No\.?|Title\s+Order\s*(?:#|No\.?))\s*:?\s*[A-Za-z0-9][\w\-]*"
)
# ONE identity item anchored to the END of the searched span. A run of several items
# ("TS No <x> TO No <y>") is peeled one at a time rather than matched by a single
# `(?:ITEM)(?:\s+ITEM)*$` regex: that nested quantifier backtracks catastrophically —
# measured here, 200 repeated "TS No X " tokens followed by one non-matching word hung
# for over two minutes, which a hostile or merely malformed legals PDF could trigger in
# the crawler worker. Peeling is linear and needs no backtracking across items.
_PREHEADER_ONE = re.compile(rf"(?:(?<=\s)|^)({_PREHEADER_ITEM})\s*$", re.I)

# A real identity run is a few dozen characters ("TS No WA08000007-26-1 TO No
# 260032878-WA-MSI"). Bounding the scan keeps the cost independent of block size — the
# largest real block in the Test 4 issue is ~43k chars of swallowed newspaper chrome.
_PREHEADER_WINDOW = 400

# One layout prints "AMENDED NOTICE OF TRUSTEE'S SALE", so the split leaves the run as
# "TS No: <x> AMENDED" and it no longer ends on an identity item. MEASURED before being
# allowed: across the six Snohomish Tribune issues plus the King and 2025-12-17 fixtures,
# 13 of the 14 distinct pre-header runs end exactly on the identity items and this is the
# only qualifier that appears. Kept to what was observed — widen it only against real
# text, since a loose grammar here is what puts one notice's number on another.
_PREHEADER_QUALIFIER = re.compile(r"(?:(?<=\s)|^)(AMENDED)\s*$", re.I)


def _identity_run_start(text: str) -> int:
    """Index where the trailing run of identity labels begins (``len(text)`` if none).

    Any trailing qualifier is skipped first but stays INSIDE the returned run, so the
    carried text reassembles the notice's real opening ("TS No: <x> AMENDED" + header).
    """
    end = len(text)
    floor = max(0, len(text) - _PREHEADER_WINDOW)
    qualifier = _PREHEADER_QUALIFIER.search(text, floor, end)
    if qualifier:
        end = qualifier.start(1)
    peeled = False
    while end > floor:
        m = _PREHEADER_ONE.search(text, floor, end)
        if not m:
            break
        end = m.start(1)
        peeled = True
    # A bare qualifier with no identity items in front of it is just a word — not a run.
    return end if peeled or not qualifier else len(text)


def _trailing_identity(text: str) -> str:
    """The identity run butted against the end of ``text``, or "" when there is none."""
    return text[_identity_run_start(text):].strip()


def _detach_trailing_identity(block: str) -> tuple[str, str]:
    """Split ``block`` into (body, the trailing identity run).

    Detaching is unconditional; whether the run is actually USED is decided at the
    receiving end by ``split_notice_blocks``, which is the stronger test — see there.
    """
    start = _identity_run_start(block)
    if start >= len(block):
        return block, ""
    return block[:start].rstrip(), block[start:].strip()


# Deliberately looser than the parser's `_TS_NUMBER`: this only answers "does the body
# still identify itself?", so a permissive label match is the safe side here.
_ANY_TS_LABEL = re.compile(
    r"(?<![A-Za-z])(?:T\.?S\.?\s*(?:#|No\.?(?![A-Za-z]))|Trustee\s+Sale\s+(?:No\.?|Number))"
    r"\s*:?\s*[A-Za-z0-9]",
    re.I,
)


def split_notice_blocks(normalized: str) -> list[str]:
    """Split normalized PDF text into individual NTS notice blocks.

    Each returned block starts at its own identity preamble (when the trustee prints
    one) followed by the ``NOTICE OF TRUSTEE'S SALE`` header; the listing preamble and
    inter-notice chrome are dropped. Splitting BEFORE field parsing is what bounds the
    per-field lazy regexes to a single notice so they can't drift across a boundary
    (Codex). The caller validates each block with ``is_valid_nts``.

    The identity preamble is carried forward rather than left where a header-only split
    puts it — see ``_PREHEADER_ONE`` for why that matters.

    A carried run is applied ONLY to a notice that does not already state a TS number of
    its own. That is the whole point of the run: it SUPPLIES an identity to a notice that
    prints one before its header, and must never OVERRIDE one printed after it. Without
    that test the repair reintroduced the original bug in mirror image (Codex, verified):
    a notice whose trailer happened to end on its own "TS No <x>" pushed that number onto
    the next notice, and chrome before the first header ending in a TS-looking token
    hijacked the first notice.
    """
    parts = _NOTICE_SPLIT.split(normalized)
    blocks: list[str] = []
    carry = ""  # identity run that may belong to the NEXT block's notice
    for i, part in enumerate(parts):
        if not _HAS_HEADER.search(part):
            # Text before the first header: newspaper chrome, dropped — except a
            # trailing identity run, which may introduce the notice whose header
            # follows it (only used if that notice states no TS number itself).
            carry = _trailing_identity(part)
            continue
        body, next_carry = (
            _detach_trailing_identity(part.strip())
            if i < len(parts) - 1
            else (part.strip(), "")  # nothing follows the last notice to carry into
        )
        if carry and not _ANY_TS_LABEL.search(body):
            body = f"{carry} {body}"
        blocks.append(body.strip())
        carry = next_carry
    return blocks
