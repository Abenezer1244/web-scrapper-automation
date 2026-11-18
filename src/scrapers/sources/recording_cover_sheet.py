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
    # Letters only after "Number": a text layer can glue the value on ("...Number101894020006036").
    r"Assessor\W?s\s*(?:Property\s*Tax\s*)?(?:15\s*-?\s*Digit\s*)?Parcel(?:\s*/\s*Account)?\s*(?:Number|No)[A-Za-z]*"
    r"|Tax\s*Parcel\s*(?:/\s*Account\s*)?(?:Number|No|ID)[A-Za-z]*(?:\s*\(\s*s\s*\))?",
    re.I,
)
_UNASSIGNED = re.compile(r"not\s*yet\s*assigned", re.I)
_INSTRUMENT = re.compile(r"\d{4}-\d{6}")  # a Benton recording number, never a parcel
_FRAGMENT = re.compile(r"(?<![\w-])\d[\d-]{4,}(?!\w)")  # 5+ digits standing alone, dashes allowed
_LONG_RUN = re.compile(r"\d(?:-?\d){8,}")  # 9+ digits anywhere, glued or not
_OCR_DIGITS = str.maketrans("OoQDIl|SsBZ", "00001115582")  # letters a text layer gives for digits
_BLANK_PROOF_LINES = 6  # lines after a blank parcel field that must hold no number for "none"
# A line that opens another labelled field ends the parcel field even when it carries a
# number (Codex). "Additional parcels: <n>" is not one: it continues the field.
_OTHER_FIELD = re.compile(
    r"^\s*(?:Grantor|Grantee|Reference|Document|Legal|Abbreviated|Return|Instrument|Recording|Auditor)"
    r"[^:\n]{0,60}:", re.I)
# A filer's way of writing "no parcel" (2026-023977 wrote "N/A"): the field is blank.
_NO_VALUE = re.compile(r"^\s*(?:N\s*/?\s*A\.?|NONE|-+|—+)\s*$", re.I)
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


def _parcels_within(run: str) -> set[str]:
    """Every Benton-shaped parcel a digit run could hold: the run itself, or any 15-digit
    window of a longer run (a joined wrap can carry a neighbour's digits, "7" +
    "127981020005003", Codex). Only ever used to send a record to review."""
    digits = run.replace("-", "")
    if len(digits) <= 15:
        return {p} if (p := _benton(digits)) else set()
    return {digits[k:k + 15] for k in range(len(digits) - 14) if digits[k] == "1"}


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
            # Past blank lines and "N/A"-style markers: any parcel-shaped number after them
            # is still this field's (read, then judged), except the recording's own
            # instrument number, which some sheets print after a gap ("2026-027909").
            nxt = next((ln for ln in lines[j + 1:] if ln.strip() and not _NO_VALUE.match(ln.strip(" :"))), "")
            if not any(not _INSTRUMENT.fullmatch(t) for t in _TOKEN.findall(nxt)):
                return values, False
            j += 1
            continue
        if _LABEL.search(line) and not _UNASSIGNED.search(line):
            return values, False
        if _OTHER_FIELD.match(line):
            return values, False  # "Reference Number: 2025027909" is another field, number or not
        if _BOUNDARY.search(line) and not _DIGIT_RUN.search(line):
            return values, False
        if len(values) > _MAX_FIELD_LINES:
            return values, True
        values.append(line)
        j += 1
    return values, False


def read_cover_sheet_parcel(text: str, prefix: str | None = None,
                            instrument: str | None = None) -> CoverSheetParcel:
    """The parcel the cover sheet names (see the module docstring for the outcomes)."""
    # Every dash a text layer may emit ("1–2798–102…") is the parcel's hyphen (Codex).
    lines = re.sub(r"[‐-―−]", "-", text).split("\n")
    candidates: list[str] = []
    stray = False
    cut = False
    written = False
    evidence: list[str] = []
    labelled = False
    nearby_number = False  # a parcel-shaped number just after a label, read or not
    for i, line in enumerate(lines):
        m = _LABEL.search(line)
        if not m or _UNASSIGNED.search(line):  # the checkbox text, not a field
            continue
        labelled = True
        window = [line[m.end():], *lines[i + 1:i + 1 + _BLANK_PROOF_LINES]]
        # Free-standing digit runs of 5+, so a parcel wrapped over two lines counts too.
        # Also any 9+ digit run, even glued to text ("AdditionalParcelNumber1018...", Codex).
        nearby_number = nearby_number or any(
            not _INSTRUMENT.fullmatch(t.strip("-"))
            for ln in window for t in (*_FRAGMENT.findall(ln), *_LONG_RUN.findall(ln)))
        values, cut_short = _field_values(lines, i, m.end())
        cut = cut or cut_short  # part of a field is never enough to pick a parcel, or to say "none"
        stray = stray or cut_short
        evidence.append(" | ".join(v.strip() for v in [line[:m.end()], *values] if v.strip())[:200])
        for v in values:
            v = v.strip(" :")
            if not v or _UNASSIGNED.search(v) or _NO_VALUE.match(v):
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
    # Every Benton-shaped parcel anywhere on the page, whatever the layout: the page as a
    # whole is the last word on "exactly one" and on "none". Per-layout rules (Codex
    # rounds 2-12) each missed another wrap, so this reads the page as one digit stream:
    # the recording's OWN instrument number taken out (it is printed on the page and is
    # never a parcel), then every space, line break and dash removed, so wraps, spaced
    # groups and doubled dashes all close up; letters and other punctuation still
    # separate numbers. Any 15-digit window starting with 1 in a run counts. Extra
    # candidates can only send a record to review, never attach one.
    # The instrument number is removed only where it fills a line by itself, as the
    # sheet prints it: anywhere else it might be part of a longer number (Codex).
    page = "\n".join(" " if instrument and ln.strip() == instrument else ln for ln in lines)
    stream = re.sub(r"[\s-]+", "", page)
    # Read twice: as printed, and with the letters OCR confuses for digits ("1O2") taken
    # as digits, so a garbled handwritten parcel is not mistaken for no parcel (Codex).
    misread = stream.translate(_OCR_DIGITS)
    on_page = {p for s in (stream, misread) for run in re.findall(r"\d{15,}", s)
               for p in _parcels_within(run)}
    if not distinct:
        if on_page:
            return CoverSheetParcel("ambiguous", candidates=sorted(on_page),
                                    evidence=f"{note} || a parcel elsewhere on the page")
        if cut:
            return CoverSheetParcel("ambiguous", evidence=f"{note} || field longer than read")
        if not written and nearby_number:
            # "none" needs proof the sheet names no parcel: a parcel-shaped number right
            # after the label, however the layout wrapped it, means it may (Codex).
            return CoverSheetParcel("ambiguous", evidence=f"{note} || a number follows the blank field")
        if not written and prefix:
            # The sheet names no parcel but the index shows one: never "none", which
            # would let an owner-name search attach some other property (Codex P2).
            return CoverSheetParcel("ambiguous", evidence=f"{note} || index prefix {prefix}, sheet blank")
        # Nothing parcel-shaped stands alone: noise, or numbers glued to text.
        return CoverSheetParcel("unreadable" if written else "none", evidence=note)
    if stray or len(distinct) > 1 or _benton(distinct[0]) is None:
        return CoverSheetParcel("ambiguous", candidates=distinct, evidence=note)
    parcel = distinct[0]
    if on_page - {parcel}:
        return CoverSheetParcel("ambiguous", candidates=sorted({parcel, *on_page}),
                                evidence=f"{note} || another parcel elsewhere on the page")
    if prefix and not parcel.startswith(prefix):
        return CoverSheetParcel("ambiguous", candidates=distinct,
                                evidence=f"{note} || index prefix {prefix} disagrees")
    return CoverSheetParcel("found", parcel=parcel, candidates=distinct, evidence=note)
