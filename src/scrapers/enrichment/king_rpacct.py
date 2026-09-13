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
import re
import zipfile
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


def download_extract(dest: Path, timeout: int = 300) -> str:
    """Download the extract to ``dest``; return its Last-Modified date (YYYY-MM-DD).

    Refuses anything that is not a zip holding the expected CSV: a moved file or an
    error page must fail loudly, not look like "no parcel has a mailing address".
    """
    resp = safe_get(RPACCT_URL, headers={"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"},
                    timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"King RPAcct download returned HTTP {resp.status_code}")
    body = resp.content
    if not body.startswith(b"PK"):
        raise RuntimeError("King RPAcct download is not a zip file")
    dest.write_bytes(body)
    with zipfile.ZipFile(dest) as zf:
        _csv_name(zf)
    modified = resp.headers.get("Last-Modified")
    snapshot = parsedate_to_datetime(modified).date().isoformat() if modified else "unknown"
    _logger.info("King RPAcct extract downloaded: %d bytes, snapshot %s", len(body), snapshot)
    return snapshot


def _csv_name(zf: zipfile.ZipFile) -> str:
    names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
    if len(names) != 1:
        raise RuntimeError(f"King RPAcct zip should hold one CSV, found {names}")
    return names[0]


def pin_of(major: str, minor: str) -> str:
    """The 10-digit PIN BridgeLeads stores as King's parcel_id (Major 6 + Minor 4)."""
    return (major or "").strip().zfill(6) + (minor or "").strip().zfill(4)


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
            if pin in pins:
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
    """Formatting-insensitive key: two accounts naming one address are one answer."""
    street, _, rest = address.partition(",")
    zip5 = re.search(r"\b(\d{5})\b", rest)
    return " ".join(re.sub(r"[^A-Z0-9# ]", " ", street.upper()).split()) + "|" + (
        zip5.group(1) if zip5 else rest.strip().upper())


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
