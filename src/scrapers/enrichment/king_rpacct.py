"""King County taxpayer mailing addresses from the Assessor's bulk account extract.

WHY
---
The per-parcel tax-bill page (payment.kingcounty.gov, reached through eRealProperty)
is the only other King mailing source, and King rate-blocks it: one 16,630-parcel job
on 2026-09-13 deferred every lookup and put the source in cooldown for an hour. The
Assessor also publishes the same taxpayer mailing block for every parcel as a free
weekly download, so a whole backlog resolves from one file instead of thousands of
paced page loads.

    Real Property Account.zip -> EXTR_RPAcct_NoName.csv
    AcctNbr, Major, Minor, AttnLine, AddrLine, CityState, ZipCode, ...

The taxpayer NAME is redacted in this file (RCW 42.56.070); only the address is used.
Verified 2026-09-13 against the live tax-bill page for two parcels whose stored value
disagreed: the extract matched the county both times.

WHAT IT IS NOT
--------------
A snapshot, not event-time truth: every write stamps the extract's Last-Modified date.
AttnLine is an addressee (a person, a trustee, a department), never part of the street.
A parcel can carry more than one account; an address is returned only when every
account for that parcel names the same address. Anything else is "ambiguous" and
resolves to nothing, because picking one would be a guess about whose mail it is.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get

_logger = setup_logger("scraper.enrichment.king_rpacct")

RPACCT_URL = "https://aqua.kingcounty.gov/extranet/assessor/Real%20Property%20Account.zip"
SOURCE = "king_rpacct"

_REQUIRED_COLUMNS = {"Major", "Minor", "AddrLine", "CityState", "ZipCode"}
_PLACEHOLDER_RE = re.compile(r"^\s*(?:UNKNOWN|NONE|N/?A|NULL|ADDRESS UNKNOWN)\s*$", re.I)


@dataclass(frozen=True)
class Answer:
    """What the extract says about one parcel."""

    status: str                 # found | ambiguous | no_address | absent
    mailing_address: str | None = None


def download_zip(url: str, dest: Path, check: Callable[[zipfile.ZipFile], object],
                 label: str, timeout: int = 300) -> str:
    """Download a King Assessor extract zip to ``dest``; return its Last-Modified date.

    Shared by every Assessor extract. ``check`` must raise when the zip does not hold
    the expected member: a moved file or an error page must fail loudly, not look like
    "the county has no data for any parcel".
    """
    resp = safe_get(url, headers={"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"}, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"{label} download returned HTTP {resp.status_code}")
    body = resp.content
    if not body.startswith(b"PK"):
        raise RuntimeError(f"{label} download is not a zip file")
    dest.write_bytes(body)
    with zipfile.ZipFile(dest) as zf:
        check(zf)
    modified = resp.headers.get("Last-Modified")
    snapshot = parsedate_to_datetime(modified).date().isoformat() if modified else "unknown"
    _logger.info("%s extract downloaded: %d bytes, snapshot %s", label, len(body), snapshot)
    return snapshot


def download_extract(dest: Path, timeout: int = 300) -> str:
    """Download the RPAcct extract to ``dest``; return its Last-Modified date (YYYY-MM-DD)."""
    return download_zip(RPACCT_URL, dest, _csv_name, "King RPAcct", timeout=timeout)


# Worker-local cache. The file changes weekly, so one download a day per container is
# plenty, and a job must never pay an 18 MB download per run.
_CACHE_DIR = Path(tempfile.gettempdir()) / "bridgeleads_king_rpacct"
_CACHE_MAX_AGE_S = 24 * 3600
# A failed refresh may keep using the file on disk, but not forever: past two weekly
# releases the snapshot is too old to present as the owner's current mailing address,
# and the per-parcel pages are the better answer (Codex P1).
_STALE_LIMIT_S = 14 * 24 * 3600


def _reraise_time_limit(exc: BaseException) -> None:
    """Celery's SoftTimeLimitExceeded subclasses Exception; a catch-all must not eat it."""
    if type(exc).__name__ in ("SoftTimeLimitExceeded", "TimeLimitExceeded"):
        raise exc


def cached_extract(max_age_s: float = _CACHE_MAX_AGE_S) -> tuple[Path, str] | None:
    """(zip path, snapshot date) for a recent extract, downloading when stale or missing.

    Returns None when there is no usable file at all; the caller then falls back to the
    per-parcel tax-bill pages exactly as before. A failed refresh keeps using the file
    already on disk (a days-old snapshot beats none), and says so in the log.
    """
    return cached_zip(_CACHE_DIR, "rpacct", download_extract, "King RPAcct", max_age_s=max_age_s)


def cached_zip(cache_dir: Path, stem: str, download: Callable[[Path], str], label: str,
               max_age_s: float = _CACHE_MAX_AGE_S) -> tuple[Path, str] | None:
    """The worker-local cache behind every King Assessor extract (see cached_extract)."""
    zip_path = cache_dir / f"{stem}.zip"
    meta_path = cache_dir / "snapshot.json"

    def _age() -> float | None:
        try:
            return time.time() - zip_path.stat().st_mtime
        except OSError:
            return None

    age = _age()
    if age is None or age >= max_age_s:
        # Unique per attempt (not per PID): threads in one process must not share it.
        tmp = cache_dir / f"{stem}.{os.getpid()}.{uuid.uuid4().hex}.part"
        meta_tmp = tmp.with_suffix(".json")
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            snapshot = download(tmp)
            # Metadata first, then the zip, each by atomic rename, so a reader never
            # sees a half-written file. A reader racing the pair can at worst label a
            # new zip with the previous date for one lookup, never a corrupt answer.
            meta_tmp.write_text(json.dumps({"snapshot": snapshot}), encoding="utf-8")
            os.replace(meta_tmp, meta_path)
            os.replace(tmp, zip_path)
        except Exception as exc:  # noqa: BLE001 -- enrichment falls back to the pages
            _reraise_time_limit(exc)
            _logger.warning("%s refresh failed: %s", label, str(exc)[:160])
        finally:
            # Also on a re-raised time limit, so repeated timeouts leave no debris.
            for leftover in (tmp, meta_tmp):
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    pass
        age = _age()
    if age is None or age >= _STALE_LIMIT_S:
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        snapshot = str(meta.get("snapshot") or "unknown") if isinstance(meta, dict) else "unknown"
    except (OSError, ValueError):
        snapshot = "unknown"
    return zip_path, snapshot


def resolve_pins(pins: set[str]) -> tuple[dict[str, Answer], str] | None:
    """Answers for ``pins`` from the cached extract, or None when no extract is usable."""
    cached = cached_extract()
    if cached is None or not pins:
        return None
    zip_path, snapshot = cached
    try:
        accounts = load_accounts(zip_path, pins)
    except Exception as exc:  # noqa: BLE001 -- a bad file must not break enrichment
        _reraise_time_limit(exc)
        _logger.warning("King RPAcct read failed: %s", str(exc)[:160])
        return None
    return {pin: resolve(accounts.get(pin)) for pin in pins}, snapshot


_ACCOUNT_RE = re.compile(r"\d{12}")


def resolve_account_pins(accounts: set[str]) -> tuple[dict[str, str], str] | None:
    """{12-digit account number: 10-digit PIN} from the cached extract, or None without one.

    King's recorder sometimes prints the parcel's 12-digit tax ACCOUNT number where the
    10-digit PIN belongs ("PID: 012603938700"). An account the extract does not carry is
    simply absent from the answer: this never guesses which digits to drop.
    """
    cached = cached_extract()
    if cached is None or not accounts:
        return None
    zip_path, snapshot = cached
    try:
        return load_account_pins(zip_path, accounts), snapshot
    except Exception as exc:  # noqa: BLE001 -- a bad file must not break enrichment
        _reraise_time_limit(exc)
        _logger.warning("King RPAcct account read failed: %s", str(exc)[:160])
        return None


def load_account_pins(zip_path: Path, accounts: set[str]) -> dict[str, str]:
    """The PIN behind each requested account number, only when the extract is unanimous.

    Measured 2026-09-14: all 739,983 rows carry a 12-digit AcctNbr whose first 10 digits
    equal Major+Minor, and the 537 repeated accounts are one parcel split into a taxable
    and an exempt row. A row that breaks either fact, or an account naming two PINs,
    resolves to nothing rather than to a guess.
    """
    wanted = {a for a in accounts if _ACCOUNT_RE.fullmatch(a or "")}
    seen: dict[str, set[str | None]] = {}
    with zipfile.ZipFile(zip_path) as zf, zf.open(_csv_name(zf)) as fh:
        reader = csv.DictReader(io.TextIOWrapper(fh, encoding="latin-1", newline=""))
        missing = {"AcctNbr", "Major", "Minor"} - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(f"King RPAcct schema changed, missing columns: {sorted(missing)}")
        for row in reader:
            acct = (row["AcctNbr"] or "").strip()
            if acct not in wanted:
                continue
            major, minor = (row["Major"] or "").strip(), (row["Minor"] or "").strip()
            exact = len(major) == 6 and len(minor) == 4 and pin_of(major, minor) == acct[:10]
            seen.setdefault(acct, set()).add(acct[:10] if exact else None)
    return {acct: next(iter(pins)) for acct, pins in seen.items()
            if len(pins) == 1 and None not in pins}


def _csv_name(zf: zipfile.ZipFile) -> str:
    names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
    if len(names) != 1:
        raise RuntimeError(f"King RPAcct zip should hold one CSV, found {names}")
    return names[0]


def pin_of(major: str, minor: str) -> str | None:
    """The 10-digit PIN BridgeLeads stores as King's parcel_id (Major 6 + Minor 4).

    None for a malformed row: zero-padding a non-numeric or over-long value could make it
    collide with a real parcel and attach another owner's address (Codex P2).
    """
    major, minor = (major or "").strip(), (minor or "").strip()
    if not (major.isdigit() and minor.isdigit() and len(major) <= 6 and len(minor) <= 4):
        return None
    return major.zfill(6) + minor.zfill(4)


def load_accounts(zip_path: Path, pins: set[str]) -> dict[str, list[dict[str, str]]]:
    """Every account row for the requested PINs. Streams; memory scales with ``pins``."""
    out: dict[str, list[dict[str, str]]] = {}
    with zipfile.ZipFile(zip_path) as zf, zf.open(_csv_name(zf)) as fh:
        reader = csv.DictReader(io.TextIOWrapper(fh, encoding="latin-1", newline=""))
        missing = _REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(f"King RPAcct schema changed, missing columns: {sorted(missing)}")
        for row in reader:
            pin = pin_of(row["Major"], row["Minor"])
            if pin is not None and pin in pins:
                out.setdefault(pin, []).append(
                    {k: row.get(k) or "" for k in ("AddrLine", "CityState", "ZipCode")})
    return out


def format_mailing(row: dict[str, str]) -> str | None:
    """One account's mailing address as "STREET, CITY, ST ZIP", or None if it has none.

    CityState is space-padded ("BELLEVUE  WA"); a trailing two-letter token is the state.
    A foreign address carries its country in CityState ("CANADA") and a zero ZIP, so the
    country is kept and the zero ZIP dropped rather than inventing a US format for it.
    """
    street = " ".join((row.get("AddrLine") or "").split())
    if not street or _PLACEHOLDER_RE.match(street):
        return None
    locality = " ".join((row.get("CityState") or "").split())
    if not locality:
        # A street with nowhere to send it is not a mailing address (Codex P2).
        return None
    zipcode = re.sub(r"\s+", "", row.get("ZipCode") or "")
    if not zipcode.strip("0"):
        zipcode = ""
    elif re.fullmatch(r"\d{9}", zipcode):
        zipcode = f"{zipcode[:5]}-{zipcode[5:]}"
    m = re.fullmatch(r"(.+?)\s+([A-Z]{2})", locality)
    if m:
        tail = f"{m.group(1)}, {m.group(2)}"
    else:
        tail = locality
    if zipcode:
        tail = f"{tail} {zipcode}".strip()
    return f"{street}, {tail}" if tail else street


def _identity(address: str) -> str:
    """Formatting-insensitive key: two accounts naming one address are one answer.

    Street, locality and ZIP5 all take part (Codex P2): one street in two cities is two
    addresses. ZIP+4 is ignored because accounts spell one address with and without it.
    An account with no address is not an answer and does not make a parcel ambiguous.
    """
    norm = " ".join(re.sub(r"[^A-Z0-9# ]", " ", address.upper()).split())
    tail = re.search(r"\s(\d{5})(?:\s\d{4})?$", norm)
    body = norm[: tail.start()] if tail else norm
    return body + "|" + (tail.group(1) if tail else "")


def resolve(accounts: list[dict[str, str]] | None) -> Answer:
    """The single mailing address all of a parcel's accounts agree on, if there is one."""
    if not accounts:
        return Answer("absent")
    formatted = [a for a in (format_mailing(r) for r in accounts) if a]
    if not formatted:
        return Answer("no_address")
    if len({_identity(a) for a in formatted}) != 1:
        return Answer("ambiguous")
    return Answer("found", formatted[0])
