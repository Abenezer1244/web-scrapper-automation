"""Snohomish owner mailing addresses from the county's Assessor Roll bulk export.

WHY THIS EXISTS
---------------
Snohomish stripped every attribute column off its public ArcGIS parcel layer
(0 of 319,733 rows retain a non-null ``situsline1``/``ownername``/``taxprname``,
verified 2026-09-18) while the service still answers HTTP 200 and still matches on
parcel_id. A second server instance (``gis.snoco.org/sis``) is null the same way and
the layers that still carry data are token-gated, so the public GIS attribute path
is treated as deliberately stripped and permanent, not a transient outage.

PR #343 made that failure honest (an empty payload defers instead of reading as
"no mailing address"). This module is the cure: the county still publishes the same
taxpayer block as a bulk download.

THE SOURCE
----------
ArcGIS Hub item ``ee76dfa5905947cc9af1605a25cf216a`` ("Assessor Roll CSV
Collection"), ~33 MB zip, public, no auth. ``NameAddr.csv`` inside it:

    ID, PropId, parcel_number, Role, PartyName, line_1, line_2, line_3,
    city, State, zip_postal_code

``Role`` is ``Taxpayer`` or ``Owner``. Measured over the whole file 2026-09-18:
316,584 Taxpayer rows, 316,581 (100.0%) carrying a mailing street, 146,470 (46%)
of which DIFFER from the situs, and 12,495 addressed out of state. That is better
coverage than the GIS layer it replaces.

DECISIONS, AND THE EVIDENCE BEHIND THEM
---------------------------------------
* **Taxpayer, not Owner.** Taxpayer is the mailing-of-record and is exactly what
  the dead GIS config read (``taxpr*``), so this keeps the meaning of the column
  unchanged. Owner rows are 325,080 for 316,584 parcels — several per parcel — so
  picking one would be arbitrary. The answering role is recorded either way.
* **``line_1`` only.** ``line_2`` is populated on FOUR rows of 316,584 and two of
  those are "C/O <person>", an addressee NAME this codebase does not collect;
  ``line_3`` is empty file-wide. Same reasoning the GIS config gave for skipping
  ``taxprline2/3``.
* **Leading zeros.** The county's own export strips them: our ``00437860401300``
  is stored as ``437860401300``. Only 19.7% of keys are full 14-char, so an exact
  match would miss four parcels in five and report them as having no mailing
  address — recreating the bug this module exists to fix. Both sides are
  normalised. NEVER int-cast a parcel id.
* **Duplicates are resolved, not guessed.** 11 keys carry more than one Taxpayer
  row. Ten are co-taxpayers at one address (unambiguous); one genuinely disagrees
  and is returned AMBIGUOUS rather than resolved by row order (Codex).
* **Absent is not empty.** A parcel missing from the snapshot means "no address
  association in THIS revision", never "this parcel has no mailing address". The
  two get different outcomes so the caller cannot terminalise a gap (Codex).

LICENSING
---------
``disclaimer_termsofuse.txt`` in the archive cites RCW 42.56.070(8): lists of
individuals may not be used for a commercial purpose. The product owner accepted
that risk explicitly for this dataset (2026-09-19), extending the 2026-09-13
clearance for Snohomish taxpayer mailing data. Do not widen the ingest beyond the
taxpayer mailing block without a fresh decision.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from src.config import settings
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_download_to_file, safe_get

_logger = setup_logger("enrichment.snohomish_assessor_roll")

_ITEM_ID = "ee76dfa5905947cc9af1605a25cf216a"
_ITEM_META_URL = f"https://www.arcgis.com/sharing/rest/content/items/{_ITEM_ID}?f=json"
_ITEM_DATA_URL = f"https://www.arcgis.com/sharing/rest/content/items/{_ITEM_ID}/data"

_MEMBER = "NameAddr.csv"
_TAXPAYER = "Taxpayer"

# Exact header of NameAddr.csv. A schema change must FAIL the build rather than
# silently shift columns — the whole point of a canary.
_EXPECTED_HEADER = [
    "ID", "PropId", "parcel_number", "Role", "PartyName",
    "line_1", "line_2", "line_3", "city", "State", "zip_postal_code",
]

_CACHE_DIR = Path(tempfile.gettempdir()) / "bridgeleads_snohomish_roll"

# Refresh cadence. The county republishes roughly monthly; this only bounds how
# often we ASK, and the item's own `modified` timestamp is what dates the data.
_REFRESH_AFTER_S = 24 * 3600

# ─── Semantic canaries ───────────────────────────────────────────────────────
# A truncated download, an error page served as a zip, or a future revision that
# quietly empties the taxpayer block must all be REJECTED, not published. These
# are deliberately loose enough to survive ordinary county churn and tight enough
# that the 2026-09-18 collapse of the GIS layer would have tripped them instantly.
_MIN_TAXPAYER_ROWS = 250_000          # 2026-09-18 baseline 316,584
_MIN_STREET_COVERAGE = 0.99           # 2026-09-18 baseline 1.0000
# Hard ceiling on decompressed bytes read from the archive. The 100 MB HTTP cap
# bounds the DOWNLOAD; it does not bound zip expansion (Codex).
_MAX_MEMBER_BYTES = 300 * 1024 * 1024
_MAX_ROWS = 2_000_000

FOUND = "found"
ABSENT_IN_SNAPSHOT = "absent_in_snapshot"
AMBIGUOUS = "ambiguous"
SOURCE_UNAVAILABLE = "source_unavailable"

# A parcel key that is empty or all zeros after normalisation identifies nothing.
# Verified 2026-09-18: the live file contains none, so rejecting them costs nothing
# and stops a blank key ever matching a blank key.
_AMBIGUOUS_SENTINEL = "\x00ambiguous"


@dataclass(frozen=True)
class MailingAnswer:
    """One parcel's answer, with the provenance needed to retry it sanely."""

    outcome: str
    mailing_address: str | None = None
    role: str | None = None
    revision: str | None = None

    @property
    def is_found(self) -> bool:
        return self.outcome == FOUND and bool(self.mailing_address)


def normalize_parcel_key(parcel_id: str | None) -> str | None:
    """Our zero-padded parcel id -> the key the county's export actually uses.

    Digits only, leading zeros removed. Returns None for anything that does not
    identify a parcel (blank, non-numeric, all zeros) so a junk value can never
    collide with another junk value (Codex).
    """
    if not parcel_id:
        return None
    raw = str(parcel_id).strip().replace("-", "")
    if not raw or not raw.isdigit():
        return None
    stripped = raw.lstrip("0")
    return stripped or None


def _compose(row: dict) -> str | None:
    """"STREET, CITY, ST ZIP" from one NameAddr row, or None if unusable.

    line_2/line_3 are deliberately ignored — see the module docstring.
    """
    street = (row.get("line_1") or "").strip()
    if not street:
        return None
    city = (row.get("city") or "").strip()
    state = (row.get("State") or "").strip()
    zipc = (row.get("zip_postal_code") or "").strip()
    locality = ", ".join(p for p in (city, state) if p)
    if zipc:
        locality = f"{locality} {zipc}".strip()
    out = f"{street}, {locality}" if locality else street
    # results.mailing_address is String(512).
    return out[:512]


# ─── Snapshot identity ───────────────────────────────────────────────────────

def _remote_revision() -> str | None:
    """The item's own ``modified`` stamp — dates the DATA, not our copy.

    File mtime only says when we last downloaded; re-fetching identical stale
    bytes would reset it and make a frozen source look fresh (Codex).
    """
    try:
        resp = safe_get(_ITEM_META_URL, require_allowlisted=False, timeout=20)
        if resp.status_code != 200:
            return None
        meta = resp.json()
        modified = meta.get("modified")
        return str(modified) if modified else None
    except Exception as exc:  # noqa: BLE001 -- a metadata miss must not be fatal
        _logger.warning("Snohomish roll: item metadata unavailable: %s", str(exc)[:120])
        return None


def _index_path(revision: str) -> Path:
    return _CACHE_DIR / f"nameaddr.{revision}.sqlite"


def _published_meta() -> dict:
    try:
        return json.loads((_CACHE_DIR / "snapshot.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


# ─── Index build ─────────────────────────────────────────────────────────────

def _iter_taxpayer_rows(zip_path: Path):
    """Stream NameAddr.csv OUT of the archive under a decompression-bomb cap.

    The 64 MB member is never extracted to disk and never held in memory.
    """
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.rsplit("/", 1)[-1] == _MEMBER]
        if len(names) != 1:
            raise RuntimeError(f"expected exactly one {_MEMBER}, found {len(names)}")
        info = zf.getinfo(names[0])
        if info.file_size > _MAX_MEMBER_BYTES:
            raise RuntimeError(f"{_MEMBER} declares {info.file_size} bytes, over cap")
        with zf.open(names[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace")
            reader = csv.reader(text)
            header = next(reader, None)
            if header != _EXPECTED_HEADER:
                raise RuntimeError(f"{_MEMBER} header changed: {header!r}")
            for n, values in enumerate(reader):
                if n > _MAX_ROWS:
                    raise RuntimeError("row cap exceeded")
                if len(values) != len(_EXPECTED_HEADER):
                    continue
                row = dict(zip(_EXPECTED_HEADER, values, strict=True))
                if row["Role"].strip() == _TAXPAYER:
                    yield row


def _build_index(zip_path: Path, dest: Path, revision: str) -> dict:
    """Build the parcel -> mailing index, validating the WHOLE revision first.

    Nothing is published until the scan completes and passes every canary, so a
    truncated archive can never answer for the parcels it did contain (Codex).
    """
    tmp = dest.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex}.part")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    usable = 0
    answers: dict[str, tuple[str, str]] = {}
    conflicts = 0
    try:
        for row in _iter_taxpayer_rows(zip_path):
            rows += 1
            key = normalize_parcel_key(row["parcel_number"])
            if not key:
                continue
            mailing = _compose(row)
            if not mailing:
                continue
            usable += 1
            prior = answers.get(key)
            if prior is None:
                answers[key] = (mailing, _TAXPAYER)
            elif prior[0] == _AMBIGUOUS_SENTINEL:
                continue
            elif prior[0] != mailing:
                # Two taxpayer rows disagreeing about where the mail goes. Ten of
                # the 11 duplicate keys are co-taxpayers at ONE address (handled
                # by the equality above); the one that genuinely disagrees must
                # not be settled by row order.
                answers[key] = (_AMBIGUOUS_SENTINEL, _TAXPAYER)
                conflicts += 1

        if rows < _MIN_TAXPAYER_ROWS:
            raise RuntimeError(f"only {rows} taxpayer rows (min {_MIN_TAXPAYER_ROWS})")
        coverage = (usable / rows) if rows else 0.0
        if coverage < _MIN_STREET_COVERAGE:
            raise RuntimeError(
                f"mailing street coverage {coverage:.4f} below {_MIN_STREET_COVERAGE}"
            )

        con = sqlite3.connect(str(tmp))
        try:
            con.execute("CREATE TABLE mailing (k TEXT PRIMARY KEY, a TEXT, r TEXT)")
            con.executemany(
                "INSERT OR REPLACE INTO mailing (k, a, r) VALUES (?, ?, ?)",
                ((k, a, r) for k, (a, r) in answers.items()),
            )
            con.commit()
        finally:
            con.close()
        os.replace(tmp, dest)  # atomic publish
    finally:
        tmp.unlink(missing_ok=True)

    stats = {
        "revision": revision,
        "taxpayer_rows": rows,
        "usable": usable,
        "parcels": len(answers),
        "conflicts": conflicts,
        "built_at": time.time(),
    }
    (_CACHE_DIR / "snapshot.json").write_text(json.dumps(stats), encoding="utf-8")
    _logger.info(
        "Snohomish roll: indexed revision %s — %d taxpayer rows, %d parcels, "
        "%d ambiguous", revision, rows, len(answers), conflicts,
    )
    return stats


def _ensure_index() -> Path | None:
    """The path to a VALIDATED index for the current revision, or None.

    On any failure the previously published index is kept and used: a bad new
    revision must never take a good one out of service.
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    revision = _remote_revision()
    published = _published_meta()
    prior_rev = published.get("revision")
    prior = _index_path(str(prior_rev)) if prior_rev else None

    if revision and _index_path(revision).exists():
        return _index_path(revision)
    if revision is None:
        # Could not date the source. Use what we already trust rather than
        # re-downloading blind.
        return prior if (prior and prior.exists()) else None
    if prior and prior.exists() and (time.time() - float(published.get("built_at") or 0)) < _REFRESH_AFTER_S:
        return prior

    zip_tmp = _CACHE_DIR / f"roll.{os.getpid()}.{uuid.uuid4().hex}.part"
    try:
        safe_download_to_file(
            _ITEM_DATA_URL, str(zip_tmp),
            max_bytes=settings.MAX_DOWNLOAD_BYTES,
            require_allowlisted=False, timeout=180,
        )
        _build_index(zip_tmp, _index_path(revision), revision)
        return _index_path(revision)
    except Exception as exc:  # noqa: BLE001 -- keep serving the last good revision
        _logger.warning("Snohomish roll: refresh failed (%s); keeping prior index",
                        str(exc)[:160])
        return prior if (prior and prior.exists()) else None
    finally:
        zip_tmp.unlink(missing_ok=True)


# ─── Public API ──────────────────────────────────────────────────────────────

def resolve_mailing(parcel_ids: list[str]) -> dict[str, MailingAnswer]:
    """Owner/taxpayer mailing addresses for Snohomish parcels, keyed by CALLER id.

    Every requested id gets an answer, and the four outcomes are distinct on
    purpose: only ``found`` is a real address, and only ``found`` and
    ``absent_in_snapshot`` say anything authoritative about the parcel.
    ``source_unavailable`` must never be recorded as "this parcel has no mailing
    address" — that is the exact mistake this whole change exists to correct.
    """
    if not parcel_ids:
        return {}
    try:
        index = _ensure_index()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Snohomish roll: index unavailable: %s", str(exc)[:160])
        index = None
    if index is None:
        return {pid: MailingAnswer(SOURCE_UNAVAILABLE) for pid in parcel_ids}

    revision = str(_published_meta().get("revision") or "")
    out: dict[str, MailingAnswer] = {}
    # Caller ids can repeat and can spell one APN several ways; normalise once and
    # fan the answer back out to every original spelling.
    by_key: dict[str, list[str]] = {}
    for pid in parcel_ids:
        key = normalize_parcel_key(pid)
        if key is None:
            out[pid] = MailingAnswer(ABSENT_IN_SNAPSHOT, revision=revision)
        else:
            by_key.setdefault(key, []).append(pid)
    if not by_key:
        return out

    try:
        con = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Snohomish roll: index open failed: %s", str(exc)[:120])
        return {pid: MailingAnswer(SOURCE_UNAVAILABLE) for pid in parcel_ids}
    try:
        keys = list(by_key)
        rows: dict[str, tuple[str, str]] = {}
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            q = ",".join("?" * len(chunk))
            for k, a, r in con.execute(
                f"SELECT k, a, r FROM mailing WHERE k IN ({q})", chunk  # noqa: S608 -- placeholders only
            ):
                rows[k] = (a, r)
    finally:
        con.close()

    for key, callers in by_key.items():
        hit = rows.get(key)
        if hit is None:
            answer = MailingAnswer(ABSENT_IN_SNAPSHOT, revision=revision)
        elif hit[0] == _AMBIGUOUS_SENTINEL:
            answer = MailingAnswer(AMBIGUOUS, revision=revision)
        else:
            answer = MailingAnswer(FOUND, mailing_address=hit[0], role=hit[1],
                                   revision=revision)
        for pid in callers:
            out[pid] = answer
    return out
