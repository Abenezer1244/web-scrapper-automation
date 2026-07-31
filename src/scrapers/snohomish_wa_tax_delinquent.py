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
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import chain
from urllib.parse import urljoin

from src.api.middleware.security import add_scrape_domain
from src.api.tax_filters import tax_cap_min_year
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

@dataclass(frozen=True)
class _Layout:
    """Column map for one published revision of the Current Tax List.

    The county rotates the bulk file (filenames ``..._36.txt`` / ``..._39.txt``)
    and on 2026-07-01 it also CHANGED THE LAYOUT: 17 fields → 15, dropping both
    address "line 2" columns and the mailing STREET line, and adding an amount
    column. Both revisions are still served from the landing page, so the parser
    selects a layout by field count instead of assuming one.
    """

    name: str
    n_fields: int
    situs_street: int
    situs_line2: int | None
    situs_city: int
    situs_state: int
    situs_zip: int
    owner: int
    # None = this revision does not publish a mailing street line at all.
    mail_street: int | None
    mail_line2: int | None
    mail_city: int
    mail_state: int
    mail_zip: int
    as_of: int
    # Amount billed to date for the tax year (what `total_billed` has always meant).
    billed: int
    # Amount already PAID. Only set where `billed == paid + owed` is a verified
    # property of the revision — it is the semantic canary (see _INVARIANT_TOLERANCE).
    # None disables the check for that layout.
    paid: int | None
    # Amount STILL OWED. Note this is NOT the last column in either revision --
    # in v15 the last column is the full-year levy, so taking "the last amount"
    # would silently overstate every delinquent balance.
    owed: int
    # Full-year levy; only published by v15.
    levy: int | None


# Layout as served until 2026-07-01. Kept verbatim: the old file is still live at
# a second URL and its amount columns are (billed, <half>, owed) -- owed is f16.
_LAYOUT_V17 = _Layout(
    name="v17_pre_2026_07",
    n_fields=17,
    situs_street=2, situs_line2=3, situs_city=4, situs_state=5, situs_zip=6,
    owner=7,
    mail_street=8, mail_line2=9, mail_city=10, mail_state=11, mail_zip=12,
    as_of=13,
    # paid=None: v17's middle amount column is NOT "paid" — its own rows disprove
    # the balance (e.g. 117.03|60.01|60.01, where 60.01+60.01 != 117.03), so the
    # semantic canary cannot be applied to this revision.
    billed=14, paid=None, owed=16, levy=None,
)

# Layout live since 2026-07-01. Amount columns verified against the full 327,721-row
# file: the invariant billed == paid + owed holds on 327,720/327,720 rows (100.0000%),
# which is what identifies col 13 as "still owed" and col 14 as the full-year levy.
_LAYOUT_V15 = _Layout(
    name="v15_2026_07",
    n_fields=15,
    situs_street=2, situs_line2=None, situs_city=3, situs_state=4, situs_zip=5,
    owner=6,
    mail_street=None, mail_line2=None, mail_city=7, mail_state=8, mail_zip=9,
    as_of=10,
    billed=11, paid=12, owed=13, levy=14,
)

_LAYOUTS: dict[int, _Layout] = {lay.n_fields: lay for lay in (_LAYOUT_V15, _LAYOUT_V17)}

# A real-property parcel id is 14 digits; personal-property accounts are 7.
_REAL_PROPERTY_PARCEL_LEN = 14

# Bounds for values read out of the untrusted remote file.
# _MAX_AMOUNT matches the Result.delinquent_amount contract (Numeric(12, 2)) and
# the ceiling _extract_tax_fields already enforces downstream.
_MAX_AMOUNT = Decimal("99999999.99")
_CENT = Decimal("0.01")
_MIN_AS_OF_YEAR = 1900
_MAX_AS_OF_YEAR = 2200

# SEMANTIC canary. The width check and _MAX_MALFORMED_RATIO only catch a change in
# SHAPE. A revision with the SAME width but PERMUTED columns would parse silently and
# emit wrong owner/address/amount — the same class of failure as the 17->15 change
# that went unnoticed for 5 weeks. For layouts that publish `paid`, billed == paid +
# owed is a property of the data (verified: 327,720/327,720 rows, 100.0000%, on the
# live v15 file), so a column permutation breaks it immediately.
_INVARIANT_TOLERANCE = Decimal("0.01")
# Fraction of checked rows allowed to violate before the scrape fails. The real file
# violates on 0 rows, so anything above a rounding-noise floor means the columns moved.
_MAX_INVARIANT_VIOLATION_RATIO = 0.01

# As-of consensus: sample this many width-valid rows and take the majority as-of year
# rather than trusting whichever row happens to come first (a stray or stale leading
# row would otherwise redefine "delinquent" for the entire file).
_AS_OF_SAMPLE_ROWS = 200
# Hard ceiling on lines buffered while sampling, so the sampler itself can never
# become the memory-exhaustion path (a file with no valid rows would otherwise be
# buffered whole). ~5k lines is well under 1 MB and far beyond the real file's
# need: it reaches 200 valid rows within the first ~201 lines.
_MAX_HEAD_SCAN_LINES = 5_000
# A sampled head that disagrees about the as-of year means two files were spliced
# or the source is mid-rotation. Ratio-gated rather than zero-tolerance so one
# stray row can't take the connector down (measured: 0 disagreement on the live file).
_MAX_AS_OF_DISAGREEMENT_RATIO = 0.10

# TEXT-shape canary. The amount invariant only protects the amount block; a
# permutation could preserve billed == paid + owed and still swap owner/address
# columns, which is half of what the same-width-reorder risk actually is.
# Thresholds are MEASURED against the live file's 8,900 in-scope rows, not guessed:
#   owner empty 0.0000% · owner numeric-like 0 · situs state 'WA' 8,899 + '' 1
#   situs zip EMPTY 14.7865% (!) · situs zip present-but-malformed 0.0112%
# So an empty zip and an empty state are NORMAL and must not count as violations —
# requiring a well-formed zip would fail ~1 row in 7 and abort every run. Mailing
# state is deliberately NOT checked: out-of-state owners are legitimate and are
# precisely the absentee-owner signal (measured CA/AZ/TX/NV/ID/OR/FL...).
_EXPECTED_SITUS_STATE = "WA"
_ZIP_RE = re.compile(r"\d{5}(?:-\d{4})?")
_MAX_TEXT_VIOLATION_RATIO = 0.01

# Parser-local ceilings. MAX_DOWNLOAD_BYTES bounds the file, not the parse: a minimal
# valid row is ~41 bytes, so a size-capped file could still encode millions of parcels
# and blow the worker's memory through `agg`. Generous headroom over the real feed
# (327,721 rows / ~3,900 delinquent parcels) — these exist to fail loudly, not to trim.
_MAX_SOURCE_ROWS = 2_000_000
_MAX_DISTINCT_PARCELS = 500_000
# Exact published as-of widths. strptime's directives are variable-width, so these
# gate the shape before strptime validates the calendar.
_SLASH_DATE_RE = re.compile(r"\d{1,2}/\d{1,2}/\d{4}")
_COMPACT_DATE_RE = re.compile(r"\d{8}")
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
    # Bound at parse time. Every amount here comes from an untrusted remote file
    # and is summed across a parcel's years before being quantized and written to
    # enrichment_data. _extract_tax_fields bounds delinquent_amount downstream,
    # but total_billed / full_year_levy would otherwise reach the JSON unbounded,
    # so a corrupt cell could mean huge Decimal work or an oversized payload.
    # Same ceiling as the Result.delinquent_amount contract (Numeric(12, 2)).
    if d > _MAX_AMOUNT:
        return None
    # Normalise to cents. Compare NUMERICALLY so a cent-equivalent value like
    # '1.230' is accepted (Decimal('1.23') == Decimal('1.230')) while genuine
    # sub-cent precision like '1.234' is rejected. The earlier raw-exponent test
    # threw away the harmless case too, which would silently drop valid rows on a
    # cosmetic source formatting change (Codex §14 pass 2). Checked after the
    # _MAX_AMOUNT bound so quantize() can't raise on an enormous value.
    cents = d.quantize(_CENT)
    return cents if cents == d else None


def _is_number_like(raw: str) -> bool:
    """True when a text cell is really just a number. No amount semantics.

    Deliberately NOT _to_decimal(): that one is amount-specific and rejects
    anything above _MAX_AMOUNT or with sub-cent precision, so a 14-digit parcel
    number — precisely what would appear if the owner column were replaced by an
    account column — would come back as "not a number" and defeat the text canary.
    """
    s = (raw or "").strip().lstrip("$").replace(",", "")
    if not s:
        return False
    try:
        Decimal(s)
    except (InvalidOperation, ValueError):
        return False
    return True


def _as_of_year(raw: str) -> int | None:
    """Year from a Snohomish as-of date cell, else None.

    Two published formats: 'mm/dd/yyyy' (v17) and 'YYYYMMDD' (v15, live since
    2026-07-01). Returning None here is NOT benign -- the as-of year is what
    separates "delinquent" from "current", so the scraper treats an unparseable
    as-of as a hard failure rather than falling back to the wall clock (which
    would misclassify an entire tax year across a year boundary).
    """
    s = (raw or "").strip()
    # Real calendar validation, not a shape check: this cell comes from an
    # untrusted remote file and decides delinquency, so a corrupt '99/99/2027'
    # must fail closed rather than yield 2027. strptime also means a 14-digit
    # parcel or an amount can never be mistaken for a date.
    # Shape-gate BEFORE strptime. strptime's numeric directives are variable-width,
    # so '%Y%m%d' happily parses '202671' and '2026071' as 2026-07-01 — a truncated
    # or shifted cell would be silently accepted as a valid year (verified, Codex
    # §14 pass 2). The regex pins the exact published widths; strptime then does the
    # real calendar validation (rejects Feb 30, non-leap 02/29).
    if _SLASH_DATE_RE.fullmatch(s):
        fmt = "%m/%d/%Y"
    elif _COMPACT_DATE_RE.fullmatch(s):
        fmt = "%Y%m%d"
    else:
        return None
    try:
        parsed = datetime.strptime(s, fmt)
    except ValueError:
        return None
    return parsed.year if _MIN_AS_OF_YEAR <= parsed.year <= _MAX_AS_OF_YEAR else None


def _join_address(*parts: str | None) -> str | None:
    """Build a readable single-line address from Snohomish address parts.

    Accepts ``(street[, line2], city, state, zip)`` with ``None`` for columns a
    given layout does not publish. The last part is treated as the ZIP (appended
    without a comma); the two before it as city/state.
    """
    vals = [(p or "").strip() for p in parts]
    zip_code = vals.pop() if vals else ""
    state = vals.pop() if vals else ""
    city = vals.pop() if vals else ""
    street_full = " ".join(p for p in vals if p)
    locality = " ".join(p for p in (city, state) if p)
    head = ", ".join(p for p in (street_full, locality) if p)
    if zip_code:
        head = (head + " " + zip_code).strip(", ").strip()
    return BridgeScraper.clean(head)


def _address_from(f: list[str], street: int | None, line2: int | None,
                  city: int, state: int, zip_code: int) -> str | None:
    """Join one address block, honouring columns this layout does not publish."""
    parts: list[str | None] = []
    if street is not None:
        parts.append(f[street])
    if line2 is not None:
        parts.append(f[line2])
    parts += [f[city], f[state], f[zip_code]]
    return _join_address(*parts)


def _has_street(f: list[str], street: int | None, line2: int | None) -> bool:
    """True when the row actually carries a street line for this address block.

    Deliberately data-driven, not layout-driven: v15 drops the mailing street
    column outright, but v17 rows very often carry it BLANK. Both cases must be
    treated the same, because a "mailing address" of "EVERETT, WA 98201" is not
    an address -- compute_owner_flags() reads the mailing address to derive
    owner_state / absentee_owner / out_of_state_owner, and skip-trace bills per
    lookup, so a city-only value manufactures confident-looking wrong signals.
    """
    for idx in (street, line2):
        if idx is not None and f[idx].strip():
            return True
    return False


def parse_tax_list(
    lines, *, fallback_year: int, cap_min_year: int | None = None
) -> tuple[list[ScrapedRecord], dict]:
    """Parse the pipe-delimited Current Tax List into delinquent ScrapedRecords.

    Streams ``lines`` (a file iterator or any line iterable) and keeps only the
    per-parcel aggregate for delinquent real-property parcels — never the full
    325k-row set — so memory stays bounded regardless of file size.

    Delinquency = 14-digit parcel AND amount owed > 0 AND tax year < the file's
    as-of year (read from col 13; falls back to ``fallback_year``).

    ``cap_min_year`` enforces the 18-month product cap: a parcel is DROPPED when
    its oldest delinquent year (``bill_year``) is older than this year. ``None``
    (the default) disables the cap, keeping the parser pure for callers/tests
    that don't want it; the scraper passes ``tax_cap_min_year(today)``.

    Returns ``(records, stats)`` where stats = {total, malformed, delinquent_rows,
    capped_out, as_of_year} for the caller's structural-validation / canary checks.
    """
    agg: dict[str, dict] = {}
    total = 0
    malformed = 0
    delinquent_rows = 0
    current_year: int | None = None
    layout: _Layout | None = None
    invariant_checked = 0
    invariant_violations = 0
    text_checked = 0
    text_violations = 0
    as_of_rows = 0
    as_of_mismatch = 0

    # ---- head sample: decide the layout and the as-of year by CONSENSUS --------
    # Single streaming pass, so buffer only the first _AS_OF_SAMPLE_ROWS width-valid
    # rows. Taking whichever row comes first would let one stray or stale leading row
    # redefine the delinquency cutoff for the whole file (Codex §14 pass 4).
    stream = iter(lines)
    head: list[str] = []
    as_of_votes: Counter[int] = Counter()
    sampled = 0
    for line in stream:
        head.append(line)
        # Bound the buffer by LINES READ, not just by valid rows found. Without
        # this, a large file containing few or no width-valid rows would be pulled
        # into memory in its entirety before the main loop (and its
        # _MAX_SOURCE_ROWS check) ever runs — reintroducing, in the sampler, the
        # exhaustion the caps were added to prevent.
        if len(head) >= _MAX_HEAD_SCAN_LINES:
            break
        stripped = line.rstrip("\r\n")
        if not stripped.strip():
            continue
        f = stripped.split("|")
        if layout is None:
            layout = _LAYOUTS.get(len(f))
        if layout is None or len(f) != layout.n_fields:
            continue
        if not (f[0].strip().isdigit() and len(f[1].strip()) == 4 and f[1].strip().isdigit()):
            continue
        year_seen = _as_of_year(f[layout.as_of])
        if year_seen is not None:
            as_of_votes[year_seen] += 1
        sampled += 1
        if sampled >= _AS_OF_SAMPLE_ROWS:
            break
    if as_of_votes:
        current_year = as_of_votes.most_common(1)[0][0]
    as_of_disagreement = sum(as_of_votes.values()) - max(as_of_votes.values(), default=0)

    for line in chain(head, stream):
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        total += 1
        if total > _MAX_SOURCE_ROWS:
            raise RuntimeError(
                f"Snohomish tax list exceeded {_MAX_SOURCE_ROWS} rows — refusing to "
                "parse further (source size is implausible; bounding worker memory)"
            )
        f = line.split("|")
        # Lock the layout to the first row whose width we recognise, then hold it
        # for the whole file: a file that mixes widths is a wrong/half-swapped
        # download, and every off-width row counting as malformed lets the
        # caller's malformed-ratio guard abort loudly instead of parsing junk.
        if layout is None:
            layout = _LAYOUTS.get(len(f))
        if layout is None or len(f) != layout.n_fields:
            malformed += 1
            continue
        parcel = f[0].strip()
        year_s = f[1].strip()
        if not (parcel.isdigit() and len(year_s) == 4 and year_s.isdigit()):
            malformed += 1
            continue
        year = int(year_s)

        # WHOLE-FILE as-of agreement. The head sample only chooses the year; a
        # spliced file whose first rows agree would otherwise classify the entire
        # remainder against the wrong cutoff without any signal. Counted over every
        # structurally valid row, not just the in-scope slice.
        as_of_rows += 1
        if _as_of_year(f[layout.as_of]) != current_year:
            as_of_mismatch += 1

        ref_year = current_year or fallback_year

        # Real property only; skip 7-digit personal-property (business) accounts.
        if len(parcel) != _REAL_PROPERTY_PARCEL_LEN:
            continue
        # Exclude current-year and future rows — only prior years still owed are
        # genuinely delinquent. Checked BEFORE the amount so the malformed count
        # below reflects only rows we actually care about.
        if year >= ref_year:
            continue
        owed = _to_decimal(f[layout.owed])
        if owed is None:
            # An in-scope delinquent row whose amount cell won't parse is a
            # SOURCE-SHAPE problem, not a row to quietly drop — count it so the
            # caller's malformed-ratio guard can still abort (Codex §14 pass 2).
            malformed += 1
            continue
        if owed <= 0:
            continue

        # SEMANTIC canary: a same-width column permutation passes every shape check
        # above but breaks the arithmetic relationship between the amount columns.
        if layout.paid is not None:
            billed_v = _to_decimal(f[layout.billed])
            paid_v = _to_decimal(f[layout.paid])
            invariant_checked += 1
            if (
                billed_v is None
                or paid_v is None
                or abs(billed_v - (paid_v + owed)) > _INVARIANT_TOLERANCE
            ):
                invariant_violations += 1

        # TEXT-shape canary — the other half of the permutation risk: a reorder that
        # kept the amounts consistent but moved owner/address would still ship wrong
        # leads. Empty zip/state are normal here (measured), so only PRESENT values
        # are shape-checked.
        text_checked += 1
        owner_s = f[layout.owner].strip()
        situs_state_s = f[layout.situs_state].strip().upper()
        situs_zip_s = f[layout.situs_zip].strip()
        if (
            not owner_s
            or _is_number_like(owner_s)
            or (situs_state_s and situs_state_s != _EXPECTED_SITUS_STATE)
            or (situs_zip_s and not _ZIP_RE.fullmatch(situs_zip_s))
        ):
            text_violations += 1

        delinquent_rows += 1
        entry = agg.get(parcel)
        if entry is None and len(agg) >= _MAX_DISTINCT_PARCELS:
            raise RuntimeError(
                f"Snohomish tax list exceeded {_MAX_DISTINCT_PARCELS} distinct "
                "delinquent parcels — refusing to accumulate further (bounding "
                "worker memory; the real feed yields ~4k)"
            )
        if entry is None:
            has_mail_street = _has_street(f, layout.mail_street, layout.mail_line2)
            entry = {
                "owner": f[layout.owner],
                "situs": _address_from(
                    f, layout.situs_street, layout.situs_line2,
                    layout.situs_city, layout.situs_state, layout.situs_zip,
                ),
                # Only a real street-bearing address goes in mailing_address.
                "mailing": _address_from(
                    f, layout.mail_street, layout.mail_line2,
                    layout.mail_city, layout.mail_state, layout.mail_zip,
                ) if has_mail_street else None,
                # City/state/zip kept for audit even when it can't be a mailing
                # address, so the owner's locality isn't simply lost.
                "mail_locality": _address_from(
                    f, None, None,
                    layout.mail_city, layout.mail_state, layout.mail_zip,
                ),
                "as_of": f[layout.as_of].strip(),
                "years": set(),
                "amount": Decimal("0"),
                "total_billed": Decimal("0"),
                "full_year_levy": Decimal("0"),
            }
            agg[parcel] = entry
        entry["years"].add(year)
        entry["amount"] += owed
        billed = _to_decimal(f[layout.billed])
        if billed is not None:
            entry["total_billed"] += billed
        if layout.levy is not None:
            levy = _to_decimal(f[layout.levy])
            if levy is not None:
                entry["full_year_levy"] += levy

    records: list[ScrapedRecord] = []
    capped_out = 0
    for parcel, entry in agg.items():
        years_sorted = sorted(entry["years"])
        bill_year = years_sorted[0]  # oldest delinquent year = most months delinquent
        # 18-month product cap: drop the whole parcel if its OLDEST unpaid year is
        # further back than the cap allows (opt-in via cap_min_year; None = no cap).
        if cap_min_year is not None and bill_year < cap_min_year:
            capped_out += 1
            continue
        amount = entry["amount"].quantize(Decimal("0.01"))

        rec = ScrapedRecord()
        rec.parcel_id = parcel
        rec.party_name = BridgeScraper.clean(entry["owner"])
        rec.property_address = entry["situs"]
        rec.mailing_address = entry["mailing"]
        # No legal description in the Snohomish tax bulk file — leave None rather
        # than standing in the parcel number (parcel_id is its own field above).
        rec.legal_description = None
        rec.date_recorded = f"01/01/{bill_year}"
        # doc_type left None (like King tax rows): the daily-cache records filter
        # for tax_delinquent matches `doc_type IS NULL` OR keyword ILIKE patterns
        # ("TAX DELINQUENT", ...); the slug "tax_delinquent" would match neither
        # and hide these rows from /scrapers/{id}/records (Codex P2).
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
            # Which published revision this row was parsed from — so a future
            # layout change is attributable per row rather than guessed at.
            "source_layout": layout.name if layout else None,
        }
        # Owner locality when there is no deliverable mailing address (see
        # _has_street): recorded for audit, deliberately NOT mailing_address.
        if rec.mailing_address is None and entry["mail_locality"]:
            rec.enrichment_data["mailing_locality"] = entry["mail_locality"]
        if entry["full_year_levy"] > 0:
            rec.enrichment_data["full_year_levy"] = str(
                entry["full_year_levy"].quantize(Decimal("0.01"))
            )
        records.append(rec)

    stats = {
        "total": total,
        "malformed": malformed,
        "delinquent_rows": delinquent_rows,
        "capped_out": capped_out,
        "as_of_year": current_year,
        "layout": layout.name if layout else None,
        # Semantic-canary telemetry for the caller's structural validation.
        "invariant_checked": invariant_checked,
        "invariant_violations": invariant_violations,
        "text_checked": text_checked,
        "text_violations": text_violations,
        # Total as-of votes cast over the sampled head (denominator for disagreement).
        "as_of_votes": sum(as_of_votes.values()),
        # Whole-file as-of agreement, so a splice past the sampled head still shows.
        "as_of_rows": as_of_rows,
        "as_of_mismatch": as_of_mismatch,
        # >0 means the sampled head disagreed about the as-of year.
        "as_of_disagreement": as_of_disagreement,
        # Lines held in memory by the as-of sampler; bounded by _MAX_HEAD_SCAN_LINES
        # so the sampler can never become an exhaustion path of its own.
        "head_buffered": len(head),
    }
    return records, stats


class SnohomishWATaxDelinquentScraper(BridgeScraper):
    """Scrapes tax-delinquent real-property records from Snohomish County's
    Treasurer "Current Tax List" bulk file.

    Pure HTTP (no browser) — overrides the Playwright lifecycle to no-ops like
    king_wa_tax_delinquent.py. Downloads the bulk file under a hard size cap,
    parses it streaming, and returns one aggregated record per delinquent parcel.
    """

    @classmethod
    def collection_scope(cls, record_type: str):
        """SHOW descriptor — Snohomish tax delinquency comes from a bulk file."""
        from src.scrapers.doc_scope import dataset

        if record_type != "tax_delinquent":
            return None
        return dataset(
            "Collected from Snohomish County Treasurer's delinquent-tax list; "
            "recorder document-type filtering is not used."
        )

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
            # Freeze "now" ONCE (UTC, matching tax_filters.build_tax_conditions) so
            # the cap year and the fallback as-of year can't disagree across a
            # year-boundary rollover between two separate now() calls.
            _now = datetime.now(UTC)
            cap_min_year = tax_cap_min_year(_now.date())
            with open(tmp_path, encoding="utf-8-sig", errors="replace") as fh:
                records, stats = parse_tax_list(
                    fh,
                    fallback_year=_now.year,
                    cap_min_year=cap_min_year,
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
                f"malformed (>{int(_MAX_MALFORMED_RATIO * 100)}%) — possible source "
                f"change (layout={stats['layout']!r}; known widths "
                f"{sorted(_LAYOUTS)})"
            )
        # The as-of year is what separates "delinquent" from "current". If it did
        # not parse, parse_tax_list fell back to the wall-clock year — harmless in
        # mid-year, but across a year boundary it would classify the entire current
        # tax year as delinquent. Structural, so fail rather than ship bad leads.
        if stats["as_of_year"] is None:
            raise RuntimeError(
                "Snohomish tax list as-of date unparseable (layout="
                f"{stats['layout']!r}) — refusing to classify delinquency from the "
                "wall clock; the source date format likely changed"
            )
        # SEMANTIC validation. The checks above only prove the file has the right
        # SHAPE. If the county republished the same width with the columns in a
        # different order, every check so far would pass and we would emit wrong
        # owners and wrong amounts silently. The billed == paid + owed relationship
        # is a property of the data, so a permutation breaks it immediately.
        checked = stats["invariant_checked"]
        violations = stats["invariant_violations"]
        if checked and violations / checked > _MAX_INVARIANT_VIOLATION_RATIO:
            raise RuntimeError(
                f"Snohomish tax list failed its amount invariant: {violations} of "
                f"{checked} checked rows have billed != paid + owed "
                f"(layout={stats['layout']!r}) — the amount columns have almost "
                "certainly moved; refusing to emit leads with wrong balances"
            )
        # Same treatment for the non-amount columns: an amount-preserving reorder
        # that moved owner/address would otherwise ship wrong leads silently.
        text_checked = stats["text_checked"]
        text_violations = stats["text_violations"]
        if text_checked and text_violations / text_checked > _MAX_TEXT_VIOLATION_RATIO:
            raise RuntimeError(
                f"Snohomish tax list failed its text-shape checks: {text_violations} "
                f"of {text_checked} rows have an empty/numeric owner or a malformed "
                f"situs state/zip (layout={stats['layout']!r}) — the text columns have "
                "likely moved; refusing to emit leads with wrong owner or address"
            )
        # A head that cannot agree on the as-of year means two files were spliced or
        # the source is mid-rotation. Majority-vote alone would paper over that.
        # Whole-file check first: the head sample can agree while the tail is spliced.
        as_of_rows = stats["as_of_rows"]
        as_of_mismatch = stats["as_of_mismatch"]
        if as_of_rows and as_of_mismatch / as_of_rows > _MAX_AS_OF_DISAGREEMENT_RATIO:
            raise RuntimeError(
                f"Snohomish tax list as-of year is not consistent across the file: "
                f"{as_of_mismatch} of {as_of_rows} rows disagree with "
                f"{stats['as_of_year']} — refusing to classify delinquency from a "
                "mixed source"
            )
        votes = stats["as_of_votes"]
        disagreement = stats["as_of_disagreement"]
        if votes and disagreement / votes > _MAX_AS_OF_DISAGREEMENT_RATIO:
            raise RuntimeError(
                f"Snohomish tax list as-of year is not consistent: {disagreement} of "
                f"{votes} sampled rows disagree with the majority "
                f"({stats['as_of_year']}) — refusing to classify delinquency from a "
                "mixed source"
            )
        elif disagreement or as_of_mismatch:
            _logger.warning(
                "Snohomish tax list: %d of %d sampled rows disagreed on the as-of "
                "year (using majority %s) — worth checking the source",
                disagreement, votes, stats["as_of_year"],
            )
        if not records:
            raise RuntimeError(
                "Snohomish tax list parsed but found 0 delinquent real-property "
                "parcels — possible format or source change"
            )

        _logger.info(
            "Snohomish tax delinquent complete — %d bytes, %d rows (%d malformed), "
            "%d delinquent rows → %d parcels (%d capped out >18mo, "
            "cap_min_year=%d, as_of_year=%s, layout=%s)",
            n_bytes, total, malformed, stats["delinquent_rows"],
            len(records), stats["capped_out"], cap_min_year, stats["as_of_year"],
            stats["layout"],
        )
        if self.on_progress:
            self.on_progress(1, 1, len(records))
        return records

    async def __aenter__(self) -> "SnohomishWATaxDelinquentScraper":
        return self

    async def __aexit__(self, *args) -> None:
        pass
