"""The parcel a Washington recording cover sheet names, read off the recorded image.

RCW 65.04.045 requires page one of every recorded document to carry the assessor's
parcel/account number. Benton's recorder index truncates it ("Parcel: 1079810..."),
but its recorded images (free public PDFs with a text layer) print the cover sheet in
full, with the value on the line after its label:

    ASSESSOR'S PROPERTY TAX PARCEL/ACCOUNT NUMBER:
    1-2798-102-0005-003

    TAX PARCEL NUMBER(S)
    101894020006036

    Assessor's15-DigitParcelNumber:
    PropertyTaxParcelIDnumber isnotyetassigned.     <- no value: the form's checkbox text

Outcomes (the ``recording_apn_lookup`` vocabulary the AcclaimWeb counties use):

- ``found``: exactly one Benton-shaped parcel, consistent with the index's own prefix.
- ``none``: a parcel field with no value: the recording names no parcel.
- ``unreadable``: no parcel field, or a value with no free-standing parcel number (a
  handwritten entry the text layer turned into noise, digits glued to letters).
  Nothing is guessed from it.
- ``ambiguous``: several parcels, a parcel beside any other long number, a number of
  another shape, or a parcel that contradicts the index's truncated prefix.
  Which property is meant is not ours to pick.

The parcel says which property the recording references. It does not say the decedent
owned it; the enrichment reads the current assessor record from it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# The cover-sheet labels, whitespace-insensitive (the text layer drops spaces).
_LABEL = re.compile(
    r"Assessor\W?s\s*(?:Property\s*Tax\s*)?(?:15\s*-?\s*Digit\s*)?Parcel(?:\s*/\s*Account)?\s*(?:Number|No)\w*"
    r"|Tax\s*Parcel\s*(?:/\s*Account\s*)?(?:Number|No|ID)\w*(?:\s*\(\s*s\s*\))?",
    re.I,
)
_UNASSIGNED = re.compile(r"not\s*yet\s*assigned", re.I)
# Lines that end the field when they carry no number: the form's own boilerplate and
# the next field. A boundary line that DOES carry a number is read (Codex: an
# "Additional parcels: <n>" line is a second parcel, not the end of the field).
_BOUNDARY = re.compile(
    r"not\s*yet\s*assigned|Additional\w*|on\s*page|Grantor|Grantee|Legal|Reference|Document"
    r"|Abbreviated|Sign\s*here|Cover|Auditor|Recorder|RCW",
    re.I,
)
# A parcel-shaped token: digits with single dashes, standing alone (no letter, digit
# or dash glued to either side, Codex: "ABC101894020006036XYZ" is not a parcel).
_TOKEN = re.compile(r"(?<![\w-])\d(?:-?\d){8,20}(?![\w-])")
# Every run of 5+ digits (dashes allowed): each must be a valid parcel token, or the
# field holds something we cannot account for.
_DIGIT_RUN = re.compile(r"\d(?:-?\d){4,}")
# The index's truncated parcel: "Parcel: 1279810..."
_INDEX_PREFIX = re.compile(r"Parcel:\s*(\d{4,})\.\.\.", re.I)
_MAX_FIELD_LINES = 12   # a parcel field longer than this is not read as one parcel


@dataclass
class CoverSheetParcel:
    outcome: str                      # found | none | unreadable | ambiguous
    parcel: str | None = None
    candidates: list[str] = field(default_factory=list)
    evidence: str = ""                # the label and its value lines, for review


def index_prefix(text: str | None) -> str | None:
    """The digits of the index's truncated parcel ("Parcel: 1079810..."), if any."""
    m = _INDEX_PREFIX.search(text or "")
    return m.group(1) if m else None


def has_parcel_field(text: str) -> bool:
    return any(_LABEL.search(ln) and not _UNASSIGNED.search(ln) for ln in text.split("\n"))


def _benton(token: str) -> str | None:
    digits = token.replace("-", "")
    return digits if len(digits) == 15 and digits.startswith("1") else None


def _field_values(lines: list[str], i: int, label_end: int) -> tuple[list[str], bool]:
    """The label line's remainder and the lines after it, through the field's real end,
    and whether the field was cut short.

    Blank lines are skipped while the next text line is itself a parcel number (a second
    parcel after a gap, Codex P1); otherwise a blank line ends the field. A boundary line
    ends it unless it carries a number. A field still going after _MAX_FIELD_LINES lines
    is reported as cut short, so the caller never picks a parcel from part of it."""
    values = [lines[i][label_end:]]
    j = i + 1
    while j < len(lines):
        line = lines[j]
        if not line.strip():
            nxt = next((ln for ln in lines[j + 1:] if ln.strip()), "")
            if not any(_benton(t) for t in _TOKEN.findall(nxt)):
                return values, False
            j += 1
            continue
        if _LABEL.search(line) and not _UNASSIGNED.search(line):
            return values, False
        if _BOUNDARY.search(line) and not _DIGIT_RUN.search(line):
            return values, False
        if len(values) > _MAX_FIELD_LINES:
            return values, True
        values.append(line)
        j += 1
    return values, False


def read_cover_sheet_parcel(text: str, prefix: str | None = None) -> CoverSheetParcel:
    """The parcel the cover sheet names (see the module docstring for the outcomes)."""
    lines = text.split("\n")
    candidates: list[str] = []
    stray = False
    written = False
    evidence: list[str] = []
    labelled = False
    for i, line in enumerate(lines):
        m = _LABEL.search(line)
        if not m or _UNASSIGNED.search(line):  # the checkbox text, not a field
            continue
        labelled = True
        values, cut_short = _field_values(lines, i, m.end())
        stray = stray or cut_short  # part of a field is never enough to pick a parcel
        evidence.append(" | ".join(v.strip() for v in [line[:m.end()], *values] if v.strip())[:200])
        for v in values:
            v = v.strip(" :")
            if not v or _UNASSIGNED.search(v):
                continue
            written = True
            tokens = _TOKEN.findall(v)
            candidates += tokens
            # A long number that is not a whole token (glued to text, or a short
            # second value) means the field holds something unaccounted for.
            if len(_DIGIT_RUN.findall(v)) != len(tokens):
                stray = True
    if not labelled:
        return CoverSheetParcel("unreadable", evidence="no parcel field on the cover sheet")
    note = " || ".join(evidence)
    # Dashed and undashed spellings of one parcel are one candidate.
    distinct = list(dict.fromkeys(c.replace("-", "") for c in candidates))
    if not distinct:
        # Nothing parcel-shaped stands alone: noise, or numbers glued to text.
        return CoverSheetParcel("unreadable" if written else "none", evidence=note)
    if stray or len(distinct) > 1 or _benton(distinct[0]) is None:
        return CoverSheetParcel("ambiguous", candidates=distinct, evidence=note)
    parcel = distinct[0]
    if prefix and not parcel.startswith(prefix):
        return CoverSheetParcel("ambiguous", candidates=distinct,
                                evidence=f"{note} || index prefix {prefix} disagrees")
    return CoverSheetParcel("found", parcel=parcel, candidates=distinct, evidence=note)
