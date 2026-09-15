"""King County condominium UNIT site addresses from the Assessor's bulk condo extract.

WHY
---
King's public GIS parcel layer (KingCo_PropertyInfo/2) has NO feature for a condo unit
PIN, only for the complex (minor 0000). So every condo lead depended on the per-parcel
eRealProperty page for its property address, and lost it whenever that source was busy
or throttled: King pre-foreclosure fell from 98.7% property addresses (2026-09-02) to
70.3% (09-07, breaker) and 69.4% (09-13, lease denied), 45 of 47 blanks being units.
The Assessor publishes every unit's site address as a free weekly download:

    Condo Complex and Units.zip -> EXTR_CondoUnit2.csv
    Major, Minor, UnitNbr, ..., Address, ..., ZipCode

Verified 2026-09-14: Address equals the eRealProperty "Site Address" for 4 of 4 live
parcels, and the PIN is unique across all 115,653 unit rows.

WHAT IT IS NOT
--------------
Address is published as the county writes it: sometimes with the unit ("... #G204"),
sometimes without ("1743 NW 57TH ST" for unit 403). UnitNbr is NOT a postal unit ("E409"
is mailed as "#409", "206" as "#N-206"), so it is never appended; it is kept for display.
The file has no city. The complex's GIS feature supplies one, and it is used only when the
complex ZIP equals the unit ZIP. Without that corroboration nothing is written, so the
eRealProperty page can still fill the row (a street with no city cannot be skip traced).
"""
from __future__ import annotations

import csv
import io
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from src.scrapers.enrichment.king_rpacct import cached_zip, download_zip
from src.utils.logger import setup_logger

_logger = setup_logger("scraper.enrichment.king_condo_units")

CONDO_URL = "https://aqua.kingcounty.gov/extranet/assessor/Condo%20Complex%20and%20Units.zip"
SOURCE = "king_condo_unit"
_MEMBER = "EXTR_CondoUnit2.csv"
_REQUIRED_COLUMNS = {"Major", "Minor", "UnitNbr", "Address", "ZipCode"}
_CACHE_DIR = Path(tempfile.gettempdir()) / "bridgeleads_king_condo"
_ZIP_RE = re.compile(r"\d{5}")
_TRAILING_ZIP_RE = re.compile(r"\s(\d{5})$")
_STATE_RE = re.compile(r"[A-Z]{2}")


@dataclass(frozen=True)
class UnitSitus:
    """What the extract says about one unit PIN."""

    status: str                     # found | no_site_address | zip_conflict | ambiguous | absent
    street: str | None = None
    zip: str | None = None
    unit_nbr: str | None = None


@dataclass(frozen=True)
class Fill:
    """A complete, corroborated property address for one unit."""

    property_address: str
    city: str
    state: str
    zip: str


def _member(zf: zipfile.ZipFile) -> str:
    if _MEMBER not in zf.namelist():
        raise RuntimeError(f"King condo zip should hold {_MEMBER}, found {zf.namelist()}")
    return _MEMBER


def download_extract(dest: Path, timeout: int = 300) -> str:
    """Download the condo extract to ``dest``; return its Last-Modified date (YYYY-MM-DD)."""
    return download_zip(CONDO_URL, dest, _member, "King condo", timeout=timeout)


def cached_extract() -> tuple[Path, str] | None:
    """(zip path, snapshot date), refreshed daily, never older than the shared stale limit."""
    return cached_zip(_CACHE_DIR, "condo", download_extract, "King condo")


def complex_pin(pin: str) -> str:
    """The condo complex's own PIN: same major, minor 0000 (the feature King GIS carries)."""
    return pin[:6] + "0000"


def load_units(zip_path: Path, pins: set[str]) -> dict[str, list[dict[str, str]]]:
    """Every unit row for the requested PINs. Streams; memory scales with ``pins``."""
    out: dict[str, list[dict[str, str]]] = {}
    with zipfile.ZipFile(zip_path) as zf, zf.open(_member(zf)) as fh:
        reader = csv.DictReader(io.TextIOWrapper(fh, encoding="latin-1", newline=""))
        missing = _REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(f"King condo schema changed, missing columns: {sorted(missing)}")
        for row in reader:
            major, minor = (row["Major"] or "").strip(), (row["Minor"] or "").strip()
            if not (len(major) == 6 and len(minor) == 4 and (major + minor).isdigit()):
                continue
            pin = major + minor
            if pin in pins:
                out.setdefault(pin, []).append(
                    {k: row.get(k) or "" for k in ("Address", "ZipCode", "UnitNbr")})
    return out


def unit_situs(rows: list[dict[str, str]] | None) -> UnitSitus:
    """The unit's published street and ZIP, or why there is none."""
    if not rows:
        return UnitSitus("absent")
    answers = set()
    for row in rows:
        address = " ".join((row.get("Address") or "").split())
        zipcode = (row.get("ZipCode") or "").strip()
        unit_nbr = (row.get("UnitNbr") or "").strip() or None
        if not address:
            answers.add(UnitSitus("no_site_address", unit_nbr=unit_nbr))
            continue
        if not _ZIP_RE.fullmatch(zipcode):
            zipcode = ""
        tail = _TRAILING_ZIP_RE.search(address)
        if tail and tail.group(1) != zipcode:
            answers.add(UnitSitus("zip_conflict", unit_nbr=unit_nbr))
            continue
        street = address[: tail.start()] if tail else address
        answers.add(UnitSitus("found", street=street, zip=zipcode or None, unit_nbr=unit_nbr))
    if len(answers) != 1:
        return UnitSitus("ambiguous")
    return answers.pop()


def compose_fill(situs: UnitSitus, complex_gis: dict | None) -> Fill | None:
    """A full "STREET, CITY, ST ZIP" (the King GIS shape), only with corroborated locality."""
    if situs.status != "found" or not situs.street or not situs.zip or not complex_gis:
        return None
    city = str(complex_gis.get("situs_city") or "").strip()
    state = str(complex_gis.get("situs_state") or "").strip()
    gis_zip = str(complex_gis.get("situs_zip") or "").strip()
    if not city or not _STATE_RE.fullmatch(state) or gis_zip != situs.zip:
        return None
    return Fill(property_address=f"{situs.street}, {city}, {state} {situs.zip}",
                city=city, state=state, zip=situs.zip)


def resolve_units(pins: set[str]) -> tuple[dict[str, UnitSitus], str] | None:
    """Situs answers for ``pins`` from the cached extract, or None when none is usable."""
    cached = cached_extract()
    if cached is None or not pins:
        return None
    zip_path, snapshot = cached
    try:
        rows = load_units(zip_path, pins)
    except Exception as exc:  # noqa: BLE001 -- a bad file must not break enrichment
        if type(exc).__name__ in ("SoftTimeLimitExceeded", "TimeLimitExceeded"):
            raise
        _logger.warning("King condo extract read failed: %s", str(exc)[:160])
        return None
    return {pin: unit_situs(rows.get(pin)) for pin in pins}, snapshot
