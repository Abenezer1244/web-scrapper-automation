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
_DEHYPHEN_WORD = re.compile(r"([A-Za-z])-\n[ \t]*([A-Za-z])")
_DEHYPHEN_ID = re.compile(r"-\n[ \t]*(?=\w)")


def extract_pdf_text(data: bytes, *, max_pages: int = _MAX_PAGES) -> str:
    """Extract text from a real, text-based legals PDF.

    Raises ValueError on a non-PDF (no ``%PDF-`` magic), an encrypted PDF, or an
    empty/garbage input — the crawler treats that as a fetch failure, not a notice.
    """
    if not data or not data.startswith(_PDF_MAGIC):
        raise ValueError("not a PDF (missing %PDF- magic)")
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    if reader.is_encrypted:
        raise ValueError("encrypted PDF not supported")
    pages = list(reader.pages)[:max_pages]
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


def split_notice_blocks(normalized: str) -> list[str]:
    """Split normalized PDF text into individual NTS notice blocks.

    Each returned block starts at a ``NOTICE OF TRUSTEE'S SALE`` header; the listing
    preamble and inter-notice chrome are dropped. Splitting BEFORE field parsing is
    what bounds the per-field lazy regexes to a single notice so they can't drift
    across a boundary (Codex). The caller validates each block with ``is_valid_nts``.
    """
    return [b.strip() for b in _NOTICE_SPLIT.split(normalized) if _HAS_HEADER.search(b)]
