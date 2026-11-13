"""OCR a recorded Notice of Trustee's Sale (scan-only PDF) into fields with evidence.

Chelan's NTS documents are 1-bit scans with no text layer (live 2026-11-12), so the
sale date and amounts can only be read by OCR. OCR can misread a digit, and a plausible
wrong date can read with high confidence, so nothing here decides what a customer sees:
every notice is compared with its image by an operator (nts_recorder_documents, phase
4a). This module reports what was read, where, how confident tesseract was, and the
reasons to look closer.

Untrusted input: the PDF is decoded and OCR'd in a separate, killable process
(nts_ocr_child.py) that accepts one document shape only, in a work directory this
process owns and removes. No pipe is read: the child's result is a size-checked file.
A child that crashes, overruns or is killed is a rejection with a reason.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from src.scrapers.sources.nts_tacoma_index import _to_date, parse_nts_notice

_CHILD = Path(__file__).with_name("nts_ocr_child.py")
TESSERACT_BIN = os.environ.get("TESSERACT_BIN", "tesseract")
PARSER_VERSION = "nts_recorder_ocr/1"
_WALL_S = 300           # whole document
_PAGE_S = 60            # one page in tesseract
_MAX_RESULT_BYTES = 8 * 1024 * 1024
_WORKDIR_PREFIX = "nts_ocr_"
_ORPHAN_AGE_S = 3600    # work dirs a crashed worker left behind
# A critical field whose words read below this tesseract confidence (0-100) is flagged.
MIN_FIELD_CONF = 80.0
CRITICAL_FIELDS = ("ts_number", "auction_date", "principal_owing", "parcel")
# Fields kept in staging after the OCR text is cleared: never the borrower's name.
STORED_FIELDS = ("ts_number", "auction_date", "auction_time", "auction_location", "parcel",
                 "property_address", "principal_owing", "principal_owing_basis",
                 "note_amount", "deed_reference", "trustee", "beneficiary", "nod_date")
# RCW 61.24.040: ordinarily recorded at least 90 days before the sale (120 when a
# particular letter is required; an amended notice may give 45). Used as a REVIEW
# baseline only: which regime applies is not determined here.
BASELINE_MIN_DAYS = 90
# Not statutory: a sale read this far after recording is unusual enough to call out.
HEURISTIC_MAX_DAYS = 365


@dataclass
class OcrResult:
    ok: bool
    reason: str | None = None
    pages: int = 0
    words: list[list] = field(default_factory=list)  # [page, line_key, text, conf]
    pdf_sha256: str = ""


def ocr_pdf(pdf: bytes, *, tesseract: str = TESSERACT_BIN, wall_s: float = _WALL_S,
            page_s: float = _PAGE_S) -> OcrResult:
    """OCR one PDF in an isolated child. Never raises for a bad document."""
    digest = hashlib.sha256(pdf).hexdigest()
    _sweep_orphans()
    workdir = tempfile.mkdtemp(prefix=_WORKDIR_PREFIX)
    try:
        Path(workdir, "in.pdf").write_bytes(pdf)
        posix = os.name == "posix"
        proc = subprocess.Popen(  # noqa: S603 -- fixed argv, no shell
            [sys.executable, "-I", str(_CHILD), workdir, tesseract, str(page_s)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=posix,
        )
        try:
            proc.wait(timeout=wall_s)
        except subprocess.TimeoutExpired:
            _kill_tree(proc, posix, workdir)
            return OcrResult(False, "ocr timed out", pdf_sha256=digest)
        if posix:  # the child is gone; nothing it or tesseract started may outlive it
            _kill_groups(proc.pid, workdir)
        if proc.returncode != 0:
            return OcrResult(False, f"ocr process failed ({proc.returncode})", pdf_sha256=digest)
        result = Path(workdir, "result.json")
        if not result.exists() or result.stat().st_size > _MAX_RESULT_BYTES:
            return OcrResult(False, "ocr process output missing or too large", pdf_sha256=digest)
        try:
            body = json.loads(result.read_text(encoding="utf-8"))
        except ValueError:
            return OcrResult(False, "ocr process output unreadable", pdf_sha256=digest)
        if not isinstance(body, dict) or not body.get("ok"):
            reason = body.get("reason") if isinstance(body, dict) else None
            return OcrResult(False, str(reason or "ocr failed"), pdf_sha256=digest)
        words = body.get("words")
        if not isinstance(words, list) or not all(_is_word(w) for w in words):
            return OcrResult(False, "ocr process output malformed", pdf_sha256=digest)
        return OcrResult(True, None, int(body.get("pages", 0)), words, digest)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _is_word(w) -> bool:
    return (isinstance(w, list) and len(w) == 4 and isinstance(w[0], int)
            and isinstance(w[1], str) and isinstance(w[2], str)
            and isinstance(w[3], (int, float)))


def _kill_groups(child_pid: int, workdir: str) -> None:
    """POSIX: kill the child's process group and tesseract's (it leads its own; the
    child records its id in the work dir)."""
    groups = [child_pid]
    try:
        groups.append(int(Path(workdir, "tesseract.pgid").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    for pgid in groups:
        try:
            os.killpg(pgid, 9)
        except (ProcessLookupError, PermissionError):
            pass


def _kill_tree(proc: subprocess.Popen, posix: bool, workdir: str) -> None:
    """Kill the child and everything it started (tesseract), then reap it."""
    try:
        if posix:
            _kill_groups(proc.pid, workdir)
        else:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],  # noqa: S603,S607
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                           check=False)
    except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
        pass
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()


def _sweep_orphans() -> None:
    """Remove work dirs (they hold page scans) a crashed worker never cleaned up."""
    root = Path(tempfile.gettempdir())
    cutoff = time.time() - _ORPHAN_AGE_S
    for d in root.glob(f"{_WORKDIR_PREFIX}*"):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def ocr_text(words: list[list]) -> str:
    """The OCR words as lines of text, in reading order (tesseract's own line keys)."""
    lines: dict[str, list[str]] = {}
    for _page, key, text, _conf in words:
        lines.setdefault(key, []).append(text)
    return "\n".join(" ".join(ws) for ws in lines.values())


def _tok(text: str) -> str:
    """A word as printed, minus a leading "$" and trailing sentence punctuation. Digits,
    commas inside numbers and periods inside amounts are kept: "$1234,56" never equals
    "1,234.56"."""
    return text.lstrip("$").rstrip(",;:.")


def _value_tokens(name: str, value) -> list[str]:
    # The parser keeps dates and ids as printed; amounts are Decimals, printed "1,234.56".
    if name == "principal_owing":
        return [f"{value:,.2f}"]
    return [_tok(t) for t in str(value).split()]


def field_evidence(parsed: dict, words: list[list]) -> dict[str, dict]:
    """For each critical field the parser filled: its value and every OCR line where its
    words appear contiguously exactly as printed. ``min_conf`` is the lowest word
    confidence over ALL those occurrences (the parser does not say which one it read, so
    a low-confidence occurrence is never hidden behind a clean one), ``occurrences`` how
    many there are. None when the value cannot be located as printed at all."""
    lines: dict[str, list[list]] = {}
    for w in words:
        lines.setdefault(w[1], []).append(w)
    evidence: dict[str, dict] = {}
    for name in CRITICAL_FIELDS:
        value = parsed.get(name)
        if value in (None, ""):
            continue
        want = _value_tokens(name, value)
        spans = []
        for key, ws in lines.items():
            toks = [_tok(w[2]) for w in ws]
            for i in range(len(toks) - len(want) + 1):
                if toks[i:i + len(want)] == want:
                    spans.append((key, ws[i:i + len(want)]))
        evidence[name] = {
            "value": str(value),
            "min_conf": min((w[3] for _k, span in spans for w in span), default=None),
            "pages": sorted({span[0][0] for _k, span in spans}),
            "lines": [k for k, _span in spans],
            "occurrences": len(spans),
        }
    return evidence


def review_flags(parsed: dict, evidence: dict[str, dict], record_date: date) -> list[str]:
    """Reasons the reviewer must look closer. Never edits a value. An empty list is not
    proof the notice was read right: every notice is still compared with its image."""
    flags: list[str] = []
    for name in CRITICAL_FIELDS:
        if parsed.get(name) in (None, ""):
            flags.append(f"{name}: not read from the notice")
    for name, ev in evidence.items():
        if ev["min_conf"] is None:
            flags.append(f"{name}: not found as printed in the OCR words")
        elif ev["min_conf"] < MIN_FIELD_CONF:
            flags.append(f"{name}: low OCR confidence ({ev['min_conf']:.0f})")
        if ev.get("occurrences", 0) > 1:
            flags.append(f"{name}: appears {ev['occurrences']} times; confirm which one the "
                         "notice means")
    raw_sale = parsed.get("auction_date")
    sale = _to_date(raw_sale) if raw_sale else None
    if raw_sale and sale is None:
        flags.append(f"auction_date: {raw_sale!r} is not a calendar date")
    if sale is not None:
        days = (sale - record_date).days
        if days < BASELINE_MIN_DAYS:
            flags.append(f"auction_date: {days} days after recording, under the ordinary "
                         f"{BASELINE_MIN_DAYS}-day notice period (an amended notice may be "
                         "shorter); statutory compliance not determined")
        elif days > HEURISTIC_MAX_DAYS:
            flags.append(f"auction_date: {days} days after recording (unusually far; not a "
                         "statutory limit)")
    return flags


def read_notice(pdf: bytes, record_date: date, **kw) -> dict:
    """OCR + parse + evidence + flags for one recorded NTS: the staging row's content.

    ``text`` holds the borrower's name and is for the pending-review row only;
    ``fields`` (what staging keeps after review) never does.
    """
    res = ocr_pdf(pdf, **kw)
    if not res.ok:
        return {"ok": False, "reason": res.reason, "pdf_sha256": res.pdf_sha256}
    text = ocr_text(res.words)
    parsed = parse_nts_notice(text)
    evidence = field_evidence(parsed, res.words)
    return {
        "ok": True, "pdf_sha256": res.pdf_sha256, "pages": res.pages, "text": text,
        "fields": {k: (None if parsed.get(k) is None else str(parsed[k])) for k in STORED_FIELDS},
        "evidence": evidence, "flags": review_flags(parsed, evidence, record_date),
        "parser_version": PARSER_VERSION,
    }


# ── the recording's own facts, from its detail page (no OCR involved) ─────────

_RELATED_DT_RE = re.compile(r"(\d{5,})\s*\[DT\]")


def _detail_text(detail_html: str) -> str | None:
    text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", detail_html or "")))
    return text if "Doc Type:" in text else None


def related_deeds_of_trust(detail_html: str) -> list[str] | None:
    """The deed-of-trust recording numbers the detail page links ("Related Number:
    2531058 [DT]"), in order. None when the page is not a detail page."""
    text = _detail_text(detail_html)
    if text is None:
        return None
    i = text.find("Related Number:")
    if i < 0:
        return []
    found: list[str] = []
    for n in _RELATED_DT_RE.findall(text[i:]):
        if n not in found:
            found.append(n)
    return found


def corroborate(fields: dict, recording_apns: list[str] | None,
                related_dts: list[str] | None) -> tuple[dict[str, str], list[str]]:
    """Compare what OCR read with what the recording itself says (its detail page):
    the parcel against the recording's APN, the deed of trust against its related DT.
    Returns (agreements, flags). Agreement is strong evidence the OCR read those two
    right; a difference is flagged for the reviewer, and nothing is edited either way."""
    agree: dict[str, str] = {}
    flags: list[str] = []
    parcel = (fields.get("parcel") or "").replace("-", "").strip()
    if recording_apns is None:
        flags.append("recording detail page unreadable: parcel not cross-checked")
    elif not parcel:
        pass  # review_flags already reports the parcel as not read
    elif parcel in recording_apns:
        agree["parcel"] = "matches the recording's APN"
    else:
        flags.append(f"parcel {parcel} differs from the recording's APN "
                     f"{', '.join(recording_apns) or '(none)'}")
    deed = (fields.get("deed_reference") or "").strip()
    if related_dts is None:
        flags.append("recording detail page unreadable: deed of trust not cross-checked")
    elif deed and deed in related_dts:
        agree["deed_reference"] = "matches the recording's related deed of trust"
    elif deed:
        flags.append(f"deed of trust {deed} differs from the recording's related "
                     f"{', '.join(related_dts) or '(none)'}")
    return agree, flags
