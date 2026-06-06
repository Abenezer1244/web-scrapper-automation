"""Snohomish County (WA) — Tax Delinquent scraper via the Treasurer's bulk file.

Source: Snohomish County Treasurer "Current Tax List" — a pipe-delimited bulk
text export of every parcel and its current taxes due, published (no login) off
the stable landing page:
    https://www.snohomishcountywa.gov/5568/Treasurer-Public-Records

The actual file lives under /DocumentCenter/View/{id}/snohomish_tax_data_totals
and the numeric {id} ROTATES every monthly refresh, so we scrape the landing
page and resolve the current "Current Tax List" link at run time (never a
hard-coded id). Unlike King (Socrata JSON API) this is a ~45 MB file download,
so it is streamed to disk under a hard size cap — never loaded into RAM.

Extends the shipped Phase 4 tax filters (amount owed + months delinquent) to a
second county with ZERO API/UI/migration-column change: the records carry the
same structured `delinquent_amount` + `bill_year` fields King produces, tagged
with a distinct `source` so `_extract_tax_fields` source-gating trusts them.

File layout (pipe-delimited, NO header, 17 fields), confirmed against the live
file (325k rows, 44.7 MB):
    0  account/parcel  (14-digit = real property; 7-digit = personal property)
    1  tax year        (the bill year)
    2  situs street    3  situs street line 2
    4  situs city      5  situs state      6  situs zip
    7  owner name
    8  mailing line 1  9  mailing line 2
    10 mailing city    11 mailing state    12 mailing zip
    13 as-of date (mm/dd/yyyy — when the file was generated)
    14 total annual tax   15 half installment   16 amount owed / balance

Delinquent set = 14-digit real-property parcel AND tax year < the file's as-of
year AND amount owed > 0. A parcel recurs across delinquent years, so rows are
aggregated PER PARCEL: `delinquent_amount` = sum of owed across its delinquent
years, `bill_year` = the oldest (min) delinquent year = most months delinquent.

NOTE on the months-delinquent filter: WA property tax for year Y is billed in Y
(halves due Apr 30 / Oct 31 of Y). King's Phase 4 filter treats `bill_year` as a
~Jan-1 issue date; reusing the oldest tax year here is the same approximation
(same semantic family), accurate to the year, not the exact due date. Current-year
rows are intentionally EXCLUDED (conservative — they may not be overdue yet).
"""

import os
import re
import tempfile
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin

from src.api.middleware.security import add_scrape_domain
from src.config import settings
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_download_to_file, safe_get

_logger = setup_logger("scraper.snohomish_wa_tax_delinquent")

_HOST = "www.snohomishcountywa.gov"
_LANDING_URL = f"https://{_HOST}/5568/Treasurer-Public-Records"
_HEADERS = {"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"}
_SOURCE = "snohomish_county_delinquent_taxes"

# SSRF allowlist: a fixed county-gov host (never user-supplied). Registered at
# import so the worker's importlib load of this module seeds the allowlist
# (mirrors king_wa_tax_delinquent.py's add_scrape_domain at module top).
add_scrape_domain(_HOST)

# Targeted extraction of the bulk-file download anchor from the landing page.
# (A narrow href match — not general HTML parsing — disambiguated below by the
# surrounding text so the "description of the fields" twin link is excluded.)
_DOC_LINK_RE = re.compile(
    r'href="(/DocumentCenter/View/\d+/snohomish_tax_data_totals[^"]*)"', re.IGNORECASE
)

_EXPECTED_FIELDS = 17
# A real-property parcel id is 14 digits; personal-property accounts are 7.
_REAL_PROPERTY_PARCEL_LEN = 14
# Abort if more than this fraction of rows don't match the expected shape — the
# county swapped the file/layout and we'd otherwise parse the WRONG file silently.
_MAX_MALFORMED_RATIO = 0.2


def _select_current_tax_list_url(html: str, base_url: str) -> str:
    """Resolve the current "Current Tax List" download URL from the landing page.

    The page links the bulk file twice with the same filename: the data file
    ("Current Tax List") and a same-named twin ("...description of the fields...").
    The numeric id rotates monthly, so we never hard-code it — we pick the data
    link by its surrounding text and exclude the description twin. Raises if no
    candidate is found (e.g. the page layout changed) so the job fails loudly
    rather than scraping a stale hard-coded id.
    """
    candidates: list[tuple[str, str]] = []
    for m in _DOC_LINK_RE.finditer(html):
        href = m.group(1)
        window = html[max(0, m.start() - 400):m.start()].lower()
        candidates.append((href, window))

    if not candidates:
        raise ValueError(
            "Snohomish treasurer page: no 'Current Tax List' download link found "
            "(page layout may have changed)"
        )

    # Prefer the link whose preceding text names the Current Tax List and is not
    # the field-description twin.
    for href, window in candidates:
        if "description of the fields" in window:
            continue
        if "current tax list" in window:
            return urljoin(base_url, href)
    # Fallback: first link that isn't the description twin.
    for href, window in candidates:
        if "description of the fields" not in window:
            return urljoin(base_url, href)
    return urljoin(base_url, candidates[0][0])


def _to_decimal(raw: str) -> Decimal | None:
    """Parse a Snohomish amount cell to a non-negative Decimal, else None.

    The live file carries clean numerics (e.g. '507.83', '271'); strip an
    occasional '$'/comma defensively. Decimal(str(...)) keeps cent precision
    without binary-float drift.
    """
    if raw is None:
        return None
    s = raw.strip().lstrip("$").replace(",", "")
    if not s:
        return None
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or d < 0:
        return None
    return d


def _as_of_year(raw: str) -> int | None:
    """Year from a Snohomish 'mm/dd/yyyy' as-of date cell, else None."""
    s = (raw or "").strip()
    parts = s.split("/")
    if len(parts) == 3 and parts[2].isdigit() and len(parts[2]) == 4:
        return int(parts[2])
    return None


def _join_address(street: str, line2: str, city: str, state: str, zip_code: str) -> str | None:
    """Build a readable single-line address from Snohomish address parts."""
    street_full = " ".join(p for p in (street.strip(), line2.strip()) if p)
    locality = " ".join(p for p in (city.strip(), state.strip()) if p)
    head = ", ".join(p for p in (street_full, locality) if p)
    if zip_code.strip():
        head = (head + " " + zip_code.strip()).strip(", ").strip()
    return BridgeScraper.clean(head)


def parse_tax_list(lines, *, fallback_year: int) -> tuple[list[ScrapedRecord], dict]:
    """Parse the pipe-delimited Current Tax List into delinquent ScrapedRecords.

    Streams ``lines`` (a file iterator or any line iterable) and keeps only the
    per-parcel aggregate for delinquent real-property parcels — never the full
    325k-row set — so memory stays bounded regardless of file size.

    Delinquency = 14-digit parcel AND amount owed > 0 AND tax year < the file's
    as-of year (read from col 13; falls back to ``fallback_year``).

    Returns ``(records, stats)`` where stats = {total, malformed, delinquent_rows,
    as_of_year} for the caller's structural-validation / canary checks.
    """
    agg: dict[str, dict] = {}
    total = 0
    malformed = 0
    delinquent_rows = 0
    current_year: int | None = None

    for line in lines:
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        total += 1
        f = line.split("|")
        if len(f) != _EXPECTED_FIELDS:
            malformed += 1
            continue
        parcel = f[0].strip()
        year_s = f[1].strip()
        if not (parcel.isdigit() and len(year_s) == 4 and year_s.isdigit()):
            malformed += 1
            continue
        year = int(year_s)

        if current_year is None:
            current_year = _as_of_year(f[13])
        ref_year = current_year or fallback_year

        # Real property only; skip 7-digit personal-property (business) accounts.
        if len(parcel) != _REAL_PROPERTY_PARCEL_LEN:
            continue
        owed = _to_decimal(f[16])
        if owed is None or owed <= 0:
            continue
        # Exclude current-year and future rows — only prior years still owed are
        # genuinely delinquent.
        if year >= ref_year:
            continue

        delinquent_rows += 1
        entry = agg.get(parcel)
        if entry is None:
            entry = {
                "owner": f[7],
                "situs": _join_address(f[2], f[3], f[4], f[5], f[6]),
                "mailing": _join_address(f[8], f[9], f[10], f[11], f[12]),
                "as_of": f[13].strip(),
                "years": set(),
                "amount": Decimal("0"),
                "total_billed": Decimal("0"),
            }
            agg[parcel] = entry
        entry["years"].add(year)
        entry["amount"] += owed
        billed = _to_decimal(f[14])
        if billed is not None:
            entry["total_billed"] += billed

    records: list[ScrapedRecord] = []
    for parcel, entry in agg.items():
        years_sorted = sorted(entry["years"])
        bill_year = years_sorted[0]  # oldest delinquent year = most months delinquent
        amount = entry["amount"].quantize(Decimal("0.01"))

        rec = ScrapedRecord()
        rec.parcel_id = parcel
        rec.party_name = BridgeScraper.clean(entry["owner"])
        rec.property_address = entry["situs"]
        rec.mailing_address = entry["mailing"]
        rec.legal_description = parcel
        rec.date_recorded = f"01/01/{bill_year}"
        rec.doc_type = "tax_delinquent"
        rec.enrichment_data = {
            "source": _SOURCE,
            # Source-gated structured tax fields read by _extract_tax_fields.
            # Stored as a string of the exact Decimal (no float drift).
            "delinquent_amount": str(amount),
            "bill_year": bill_year,
            # Year-level detail for audit/debug (Codex: don't collapse to sum+min).
            "delinquent_years": years_sorted,
            "delinquent_year_count": len(years_sorted),
            "oldest_tax_year": bill_year,
            "total_billed": str(entry["total_billed"].quantize(Decimal("0.01"))),
            "as_of_date": entry["as_of"],
            "account_number": parcel,
            "county": "snohomish",
            "state": "WA",
        }
        records.append(rec)

    stats = {
        "total": total,
        "malformed": malformed,
        "delinquent_rows": delinquent_rows,
        "as_of_year": current_year,
    }
    return records, stats


class SnohomishWATaxDelinquentScraper(BridgeScraper):
    """Scrapes tax-delinquent real-property records from Snohomish County's
    Treasurer "Current Tax List" bulk file.

    Pure HTTP (no browser) — overrides the Playwright lifecycle to no-ops like
    king_wa_tax_delinquent.py. Downloads the bulk file under a hard size cap,
    parses it streaming, and returns one aggregated record per delinquent parcel.
    """

    def __init__(self, record_type: str = "tax_delinquent") -> None:
        super().__init__()

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        # date_from/date_to are part of the connector interface but unused here:
        # the source is a current snapshot (no per-record date), and delinquency
        # is derived from the file's own tax-year vs as-of-year, not a date range.
        del date_from, date_to

        landing = safe_get(
            _LANDING_URL,
            require_allowlisted=True,
            headers=_HEADERS,
            timeout=settings.DEFAULT_TIMEOUT,
        )
        landing.raise_for_status()
        file_url = _select_current_tax_list_url(landing.text, _LANDING_URL)
        _logger.info("Snohomish current tax list resolved → %s", file_url)

        fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="snoho_tax_")
        os.close(fd)
        try:
            n_bytes = safe_download_to_file(
                file_url,
                tmp_path,
                max_bytes=settings.MAX_DOWNLOAD_BYTES,
                require_allowlisted=True,
                require_https=True,
                headers=_HEADERS,
                timeout=120,
            )
            with open(tmp_path, encoding="utf-8-sig", errors="replace") as fh:
                records, stats = parse_tax_list(
                    fh, fallback_year=datetime.now(UTC).year
                )
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        total = stats["total"]
        malformed = stats["malformed"]
        # Structural validation: catch a silently-wrong file (county swapped the
        # layout / served an error page) — not just the zero-row case.
        if total == 0:
            raise RuntimeError(
                "Snohomish tax list download produced no rows (wrong or empty file)"
            )
        if malformed / total > _MAX_MALFORMED_RATIO:
            raise RuntimeError(
                f"Snohomish tax list format unexpected: {malformed}/{total} rows "
                f"malformed (>{int(_MAX_MALFORMED_RATIO * 100)}%) — possible source change"
            )
        if not records:
            raise RuntimeError(
                "Snohomish tax list parsed but found 0 delinquent real-property "
                "parcels — possible format or source change"
            )

        _logger.info(
            "Snohomish tax delinquent complete — %d bytes, %d rows (%d malformed), "
            "%d delinquent rows → %d parcels (as_of_year=%s)",
            n_bytes, total, malformed, stats["delinquent_rows"],
            len(records), stats["as_of_year"],
        )
        if self.on_progress:
            self.on_progress(1, 1, len(records))
        return records

    async def __aenter__(self) -> "SnohomishWATaxDelinquentScraper":
        return self

    async def __aexit__(self, *args) -> None:
        pass
