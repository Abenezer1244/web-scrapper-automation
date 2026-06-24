"""King County (WA) — Tax Delinquent scraper via Socrata Open Data API.

Source: King County Open Data — "Delinquent Taxes" dataset (Socrata id dsv3-ct3e
on data.kingcounty.gov). EVERY row in this dataset is an unpaid (delinquent)
receivable line — the dataset is pre-filtered to delinquent items. A single
parcel has MULTIPLE rows: one per charge type per bill year.

`receivable_type` is the CHARGE CATEGORY, not a delinquency flag. Decoded from
King's own dictionary (dataset dyps-vajd, "Real Property Tax Receivable
Attributes Descriptions"): R=Real Property Levy, N=Noxious Weed, V=Conservation
District, U=Surface Water Mgmt, X=Surface Water Bond, E=Fire District, F=Forest
Patrol, D=Drainage District, I=Irrigation District, C=Personal Property Certified
to Real, O=Omitted Levy, W=Open Space/Timber Withdrawal — all PRINCIPAL charges
billed on the property-tax bill — and A=Abatement (a credit/adjustment, NOT money
owed; rows show huge billed / $0 paid and MUST be excluded).

Total delinquent owed for a parcel = SUM(billed - paid) across all its included
charge lines, across all its delinquent years. This MATCHES Snohomish's
methodology (sum the balance across a parcel's delinquent years), so the
platform-wide "amount owed" filter means the same thing in both counties.

⚠️ HISTORY: a prior version filtered `receivable_type='D'`, mis-reading D as
"Delinquent" — it captured ~0.6% of delinquent parcels and reported a tiny
drainage line instead of the real balance. This file replaces that logic.

No penalty/interest is exposed by this dataset (King computes those at payment
time), so the figure is PRINCIPAL ONLY — label it "Total Delinquent Tax Balance
(principal only, excludes penalties & interest)". No browser automation — pure
HTTP GET. Owner name + address are enriched downstream via GIS + eRealProperty.
"""

import random
import re
import time
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal

import requests

from src.api.middleware.security import add_scrape_domain
from src.api.tax_filters import TAX_CAP_EXEMPT_SOURCES, tax_cap_min_year
from src.config import settings
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get

_logger = setup_logger("scraper.king_wa_tax_delinquent")

_API_URL = "https://data.kingcounty.gov/resource/dsv3-ct3e.json"
_HEADERS = {"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"}
_SOURCE = "king_county_delinquent_taxes"

add_scrape_domain("data.kingcounty.gov")

# The Socrata source has no owner column, so a tax row ships with this synthetic
# party_name until enrichment (eRealProperty) swaps in the real owner. The
# producer (`tax_placeholder_party`) and the matcher (`is_tax_placeholder_party`,
# used by the enrichment overwrite gate) MUST stay in sync — keep them together.
_TAX_PLACEHOLDER_PREFIX = "Tax Delinquent"
# Exact shape of `tax_placeholder_party` output: prefix, a "$<amount>" clause, and
# a closing "(Parcel <id>)". Requiring the dollar amount stops a real owner name
# that merely starts with the prefix from being matched. The separator is `.*?`
# (not the literal em dash) so source/DB encoding of the dash can never break it.
_TAX_PLACEHOLDER_RE = re.compile(
    r"^Tax Delinquent\b.*?\$[\d,]+ owed \(Parcel [^)]+\)$"
)


def tax_placeholder_party(amount: Decimal, parcel: str) -> str:
    """Synthetic party_name for a King tax-delinquent row (no owner at scrape)."""
    return f"{_TAX_PLACEHOLDER_PREFIX} — ${amount:,.0f} owed (Parcel {parcel})"


def is_tax_placeholder_party(party_name: str | None) -> bool:
    """True iff party_name is the placeholder `tax_placeholder_party` produces.

    Anchored full-shape match (prefix + "$amount" + "(Parcel id)"), so a real
    owner name that happens to start with "Tax Delinquent" is never clobbered.
    """
    return bool(_TAX_PLACEHOLDER_RE.match(party_name or ""))


def is_parcel_legal_placeholder(legal_description: str | None, parcel_id: str | None) -> bool:
    """True iff legal_description is the parcel-number stand-in this scraper sets.

    The King Socrata tax feed has no legal description (it's a tax-receivable roll),
    so `scrape()` stores the parcel number in `legal_description` as a placeholder
    (`rec.legal_description = parcel`). A real legal description is then available
    from eRealProperty enrichment. This predicate gates that overwrite: only a row
    whose legal_description is STILL just its own parcel is eligible to be replaced,
    so a real legal description is never clobbered (mirrors the party_name gate).
    """
    if not legal_description or not parcel_id:
        return False
    p = parcel_id.strip()
    # Require a non-empty parcel so whitespace-only values ("   ") don't collapse
    # to "" == "" and falsely report a stand-in.
    return bool(p) and legal_description.strip() == p

# Charge types whose (billed - paid) is real principal owed on the tax bill.
# Allowlist (fail-closed for money): anything NOT here and NOT abatement is an
# UNKNOWN code → excluded from the sum + alerted, never silently summed.
_CHARGE_TYPES_INCLUDED = frozenset({
    "R",  # Real Property Levy (main tax)
    "N",  # Noxious Weed Assessment
    "V",  # Conservation District fee
    "U",  # Surface Water Management fee
    "X",  # Surface Water Bond fee
    "E",  # Fire District fee
    "F",  # Forest Patrol Assessment
    "D",  # Drainage Benefit Assessment
    "I",  # Irrigation District fee
    "C",  # Personal Property Certified to Real
    "O",  # Real Property Omitted Levy
    "W",  # Open Space / Timber Withdrawal
})
_CHARGE_TYPE_ABATEMENT = "A"  # credit/adjustment — EXCLUDE from owed sum

# King real-property tax account = 12 numeric digits; parcel = first 10
# (Major 6 + Minor 4). Reject anything else (malformed / personal property).
_ACCOUNT_LEN = 12
_PARCEL_LEN = 10

# delinquent_amount is bounded by the Result column / filter contract.
_AMOUNT_MAX = Decimal("99999999.99")

_PAGE_SIZE = 5000

# Structural-canary threshold: only a scan of at least this many rows that emits
# 0 parcels is worth interrogating (a tiny scan legitimately yields nothing).
_CANARY_MIN_ROWS = 100

# Per-page fetch retries for transient Socrata failures (read timeout / 429 /
# 5xx). Backoff seconds, indexed by attempt; jittered to desync shared-IP retries.
_RETRY_BACKOFF = (1, 3, 7)
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def is_parse_break(stats: dict, n_emitted: int) -> bool:
    """True iff a 0-parcel result indicates a parse/source break, not a business empty.

    Structural canary (Codex-reviewed funnel). Only meaningful when the scan
    emitted nothing from a sizeable row set; returns False otherwise. RAISE-worthy
    when:
      * ``aggregated_parcels == 0`` — rows were scanned but NONE parsed into a
        candidate parcel, i.e. every row hit a row-level gate (malformed account,
        abatement, unknown charge type). That is schema/format drift.
      * every candidate overflowed (``overflow == aggregated_parcels > 0``) — an
        absurd-value wipeout that signals a unit change or corrupted parse.
    A zero explained by DOCUMENTED business filters (all candidates net-not-owed
    and/or past the recency cap) is legitimate and returns False — crashing on it
    is the bug that failed every King scrape before the cap exemption.
    """
    if n_emitted or stats.get("total_rows", 0) < _CANARY_MIN_ROWS:
        return False
    aggregated = stats.get("aggregated_parcels", 0)
    all_overflowed = aggregated > 0 and stats.get("overflow", 0) == aggregated
    return aggregated == 0 or all_overflowed


def _page_params(where: str, offset: int) -> dict:
    """Socrata query params for one page.

    Orders by ``:id`` — the dataset's unique, indexed system row id. This is the
    only stable key for ``$offset`` paging here: the previous
    ``account_number,bill_year`` order is NON-unique (a parcel has many
    same-account/year charge lines), so ties reorder at page boundaries and
    ``$offset`` can skip or duplicate the exact rows being summed — undercounting,
    overcounting, or flipping a parcel across the owed>0 threshold. ``:id`` is
    unique + indexed → correct paging AND ~24x faster (no server-side sort of the
    filtered set). Aggregation is order-independent (parcel-keyed over the full
    stream), so row order never affects the emitted records.
    """
    return {
        "$where": where,
        "$order": ":id",
        "$limit": _PAGE_SIZE,
        "$offset": offset,
    }


def _is_retryable(exc: Exception) -> bool:
    """True for transient HTTP failures worth re-attempting (read timeout /
    connection drop / 429 / 5xx); False for SSRF (ValueError) and non-transient
    4xx (bad query / forbidden), which a retry can't fix."""
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return exc.response.status_code in _RETRY_STATUS
    return False


def _parse_cents(raw) -> int | None:
    """Parse a Socrata zero-padded cent string to an int (cents), else None.

    Money is summed in integer cents end-to-end (never float) to avoid drift.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    if not s.isdigit():
        return None
    v = int(s)
    return -v if neg else v


def aggregate_delinquent_rows(
    rows, *, start_year: int, effective_end_year: int, cap_min_year: int | None = None
) -> tuple[list[ScrapedRecord], dict]:
    """Aggregate raw Socrata rows into one ScrapedRecord per delinquent parcel.

    Pure (no I/O) so it is unit-testable — ``rows`` is any iterable of row dicts
    (a live paginating generator in prod, a plain list in tests). Accumulates
    fully before emitting: a parcel's charge lines span API pages, so NOTHING is
    emitted mid-stream.

    ``cap_min_year`` enforces the 18-month product cap: a parcel is DROPPED when
    its oldest delinquent year (``bill_year``) is older than this year. ``None``
    (the default) disables the cap, keeping the function pure for callers/tests
    that don't want it; the scraper passes ``tax_cap_min_year(today)``.

    Returns ``(records, stats)``. ``delinquent_amount`` for a parcel =
    SUM(billed - paid) over included charge types and delinquent years, floored
    at 0 at the PARCEL total (not per line). ``bill_year`` = oldest delinquent
    year (matches Snohomish).
    """
    agg: dict[str, dict] = {}
    stats = {
        "total_rows": 0,
        "skipped_malformed_acct": 0,
        "abatement_rows": 0,
        "abatement_nonzero": 0,
        "unknown_type_rows": 0,
        "unknown_codes": set(),
        # Candidate parcels formed from >=1 INCLUDED charge line (before the
        # positive-balance / overflow / cap drops). The canary uses this to tell a
        # true parse/source break (rows scanned but NOTHING parsed -> 0 candidates)
        # from a legitimate business-empty (candidates existed but were filtered).
        "aggregated_parcels": 0,
        "net_zero_parcels": 0,  # candidate dropped: net (billed-paid) <= 0
        "overflow": 0,
        "capped_out": 0,
    }

    for item in rows:
        stats["total_rows"] += 1
        acct = (item.get("account_number") or "").strip()
        # Real-property gate: 12 numeric digits only. Quarantine the rest
        # (personal property / malformed) — never collapse them into a fake parcel.
        if len(acct) != _ACCOUNT_LEN or not acct.isdigit():
            stats["skipped_malformed_acct"] += 1
            continue

        year_raw = (item.get("bill_year") or "").strip()
        if not year_raw.isdigit():
            stats["skipped_malformed_acct"] += 1
            continue
        year = int(year_raw)
        # Range + current-year exclusion (defensive; also enforced in the $where).
        if year < start_year or year > effective_end_year:
            continue

        rtype = (item.get("receivable_type") or "").strip().upper()
        billed = _parse_cents(item.get("billed_amount"))
        paid = _parse_cents(item.get("paid_amount"))
        if billed is None or paid is None:
            stats["skipped_malformed_acct"] += 1
            continue
        owed_cents = billed - paid

        if rtype == _CHARGE_TYPE_ABATEMENT:
            stats["abatement_rows"] += 1
            if owed_cents != 0:
                stats["abatement_nonzero"] += 1
            continue  # abatement = credit, never summed as owed
        if rtype not in _CHARGE_TYPES_INCLUDED:
            stats["unknown_type_rows"] += 1
            stats["unknown_codes"].add(rtype)
            continue  # fail-closed: unknown code never enters the sum

        parcel = acct[:_PARCEL_LEN]
        entry = agg.get(parcel)
        if entry is None:
            entry = {
                "owed_cents": 0,
                "years": set(),
                "by_type_cents": defaultdict(int),
                "by_year_cents": defaultdict(int),
                "accounts": set(),
            }
            agg[parcel] = entry
        entry["owed_cents"] += owed_cents
        entry["years"].add(year)
        entry["by_type_cents"][rtype] += owed_cents
        entry["by_year_cents"][year] += owed_cents
        entry["accounts"].add(acct)

    stats["aggregated_parcels"] = len(agg)

    records: list[ScrapedRecord] = []
    for parcel, entry in agg.items():
        # Floor at the PARCEL total (not per line) — preserves partial-payment /
        # credit math within the parcel before clamping.
        total_cents = entry["owed_cents"]
        if total_cents <= 0:
            stats["net_zero_parcels"] += 1
            continue  # net not-owed (fully paid / credit-offset) = not a lead
        amount = (Decimal(total_cents) / 100).quantize(Decimal("0.01"))
        if amount > _AMOUNT_MAX:
            stats["overflow"] += 1
            continue  # quarantine absurd values rather than truncate

        years_sorted = sorted(entry["years"])
        bill_year = years_sorted[0]  # oldest delinquent year (matches Snoho)
        # 18-month product cap: drop the whole parcel if its OLDEST unpaid year is
        # further back than the cap allows (opt-in via cap_min_year; None = no cap).
        if cap_min_year is not None and bill_year < cap_min_year:
            stats["capped_out"] += 1
            continue

        rec = ScrapedRecord()
        rec.parcel_id = parcel
        # No owner name in the Socrata tax feed (and King redacts it from bulk
        # downloads). Leave party_name BLANK — honest "not provided" — rather than a
        # synthetic placeholder that reads as a fake name next to the real
        # property/tax data. The real owner name comes from skip-trace (address →
        # name + phone + email) or per-parcel eRealProperty enrichment. The
        # tax_placeholder_party/is_tax_placeholder_party helpers are kept ONLY so the
        # one-time clear-backfill can still recognize and null out historical rows.
        rec.party_name = None
        rec.legal_description = parcel
        rec.date_recorded = f"01/01/{bill_year}"
        rec.enrichment_data = {
            "source": _SOURCE,
            # Source-gated structured fields read by _extract_tax_fields.
            "delinquent_amount": str(amount),
            "bill_year": bill_year,
            "delinquent_years": years_sorted,
            "delinquent_year_count": len(years_sorted),
            "oldest_tax_year": bill_year,
            # Per-charge-type + per-year breakdown (council: keep line items for a
            # future tax-only facet; costs nothing, already parsed).
            "amount_by_charge_type": {
                t: str((Decimal(c) / 100).quantize(Decimal("0.01")))
                for t, c in sorted(entry["by_type_cents"].items())
            },
            "amount_by_year": {
                str(y): str((Decimal(c) / 100).quantize(Decimal("0.01")))
                for y, c in sorted(entry["by_year_cents"].items())
            },
            "account_numbers": sorted(entry["accounts"]),
            "county": "king",
            "state": "WA",
        }
        records.append(rec)

    return records, stats


class KingWATaxDelinquentScraper(BridgeScraper):
    """Scrapes King County delinquent-tax records from the Socrata open-data API.

    Returns one aggregated record per delinquent PARCEL: delinquent_amount =
    sum of (billed - paid) across all included charge lines and all delinquent
    years; bill_year = oldest delinquent year. No browser automation.
    """

    @classmethod
    def collection_scope(cls, record_type: str):
        """SHOW descriptor — King tax delinquency comes from a dataset, not docs."""
        from src.scrapers.doc_scope import dataset

        if record_type != "tax_delinquent":
            return None
        return dataset(
            "Collected from King County's delinquent-tax open dataset (Socrata); "
            "recorder document-type filtering is not used."
        )

    def __init__(self, record_type: str = "tax_delinquent"):
        super().__init__()

    def _fetch_page(self, params: dict, offset: int, page_num: int) -> list:
        """Fetch one Socrata page with bounded retries on transient failures.

        Retries read-timeout / 429 / 5xx with jittered backoff
        (settings.MAX_RETRIES attempts). A failure that survives all retries — or
        any non-transient error (SSRF ValueError, 4xx) — FAILS LOUD: a partial
        parcel set would ship an incomplete lead list the zero-parcel canary
        can't catch, so the job must fail rather than truncate.
        """
        last_exc: Exception | None = None
        for attempt in range(1, settings.MAX_RETRIES + 1):
            try:
                # S4: safe_http (SSRF defense-in-depth) — re-validates the fixed
                # HTTPS Socrata endpoint each attempt, disables ambient proxy,
                # refuses redirect-to-internal.
                resp = safe_get(_API_URL, params=params, headers=_HEADERS,
                                timeout=settings.DEFAULT_TIMEOUT)
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                last_exc = exc
                if attempt >= settings.MAX_RETRIES or not _is_retryable(exc):
                    break
                wait = _RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)]
                wait += random.uniform(0, 0.5)  # jitter to desync shared-IP retries
                _logger.warning(
                    "King tax page fetch failed (offset=%d page=%d attempt=%d/%d): %s "
                    "— retrying in %.1fs",
                    offset, page_num + 1, attempt, settings.MAX_RETRIES,
                    str(exc)[:120], wait,
                )
                time.sleep(wait)
        raise RuntimeError(
            f"King tax delinquent: API page fetch failed at offset {offset} "
            f"(page {page_num + 1}) after {settings.MAX_RETRIES} attempt(s) — "
            f"aborting to avoid a truncated result: {str(last_exc)[:120]}"
        ) from last_exc

    def _iter_api_rows(self, where: str):
        """Yield rows across all Socrata pages — a parcel's lines may span pages."""
        offset = 0
        page_num = 0
        while True:
            data = self._fetch_page(_page_params(where, offset), offset, page_num)
            if not data:
                break
            yield from data

            page_num += 1
            _logger.info("Fetched %d rows (page=%d, offset=%d)", len(data), page_num, offset)
            if self.on_progress:
                self.on_progress(page_num, 0, 0)

            if len(data) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE

    async def scrape(self, date_from: str, date_to: str) -> list[ScrapedRecord]:
        start_year = datetime.strptime(date_from, "%m/%d/%Y").year
        end_year = datetime.strptime(date_to, "%m/%d/%Y").year

        # INCLUDE the current year. King's dataset publishes ONLY delinquent
        # receivables, and in WA a missed first-half installment (due Apr 30)
        # accelerates the FULL year's tax to delinquent (RCW 84.56.020) — so a
        # current-year row here is a genuine, fresh, motivated-seller lead and its
        # full (billed - paid) is correctly delinquent, not a not-yet-due overstate.
        # (This differs from Snohomish, whose file lists ALL parcels and therefore
        # MUST exclude the current year to isolate the delinquent ones. The
        # cross-county AMOUNT definition stays identical — sum all charges per
        # parcel — only the delinquency-determination differs, by source shape.)
        # Cap at current_year so a future date_to can't pull not-yet-billed years.
        current_year = datetime.now().year
        effective_end = min(end_year, current_year)

        if effective_end < start_year:
            _logger.info(
                "King WA tax delinquent — requested %d-%d resolves to no billed "
                "years (current year=%d); nothing to scrape",
                start_year, end_year, current_year,
            )
            return []

        where = f"bill_year>='{start_year}' AND bill_year<='{effective_end}'"
        _logger.info(
            "King WA tax delinquent — delinquent bill years %d to %d",
            start_year, effective_end,
        )

        # 18-month recency cap. King is EXEMPT (user decision 2026-06-23): its
        # Socrata feed publishes only currently-unpaid receivables, so bill_year is
        # the levy year, not a delinquency-age signal, and the feed lags ~1.5yr — a
        # calendar cap would drop 100% of parcels. Driven by membership in
        # TAX_CAP_EXEMPT_SOURCES so ingestion and the query-side cap (tax_filters)
        # can never disagree. None disables the cap in aggregate_delinquent_rows.
        # Frozen UTC (matching tax_filters.build_tax_conditions) so it can't drift
        # mid-scrape for any non-exempt source.
        cap_min_year = (
            None if _SOURCE in TAX_CAP_EXEMPT_SOURCES
            else tax_cap_min_year(datetime.now(UTC).date())
        )

        records, stats = aggregate_delinquent_rows(
            self._iter_api_rows(where),
            start_year=start_year,
            effective_end_year=effective_end,
            cap_min_year=cap_min_year,
        )

        _logger.info(
            "King WA tax delinquent complete — %d rows scanned, %d candidate parcels, "
            "%d emitted (%d malformed-acct skipped, %d abatement rows, %d unknown-type "
            "rows, %d net-zero, %d overflow, %d capped out, cap_min_year=%s)",
            stats["total_rows"], stats["aggregated_parcels"], len(records),
            stats["skipped_malformed_acct"], stats["abatement_rows"],
            stats["unknown_type_rows"], stats["net_zero_parcels"], stats["overflow"],
            stats["capped_out"], cap_min_year,
        )
        # Alerts (not silent): these signal a possible parse/decode/source change.
        if stats["unknown_type_rows"]:
            _logger.warning(
                "King tax delinquent: %d rows with UNKNOWN receivable_type %s were "
                "EXCLUDED from owed sums — review King's receivable-type dictionary "
                "and update _CHARGE_TYPES_INCLUDED if these are real charges",
                stats["unknown_type_rows"], sorted(stats["unknown_codes"]),
            )
        if stats["abatement_nonzero"]:
            _logger.warning(
                "King tax delinquent: %d abatement (A) rows had nonzero (billed-paid) "
                "— expected ~0; possible parse/source anomaly, verify the dataset",
                stats["abatement_nonzero"],
            )
        if stats["overflow"]:
            _logger.warning(
                "King tax delinquent: %d parcels exceeded $%s and were quarantined",
                stats["overflow"], _AMOUNT_MAX,
            )
        # Structural canary (see is_parse_break): a sizeable scan that emits 0
        # parcels is a BUG only when nothing parsed into a candidate, or every
        # candidate was an absurd overflow — both signal a parse/gate break or a
        # source-format change. A zero from DOCUMENTED business filters (all
        # candidates net-not-owed and/or past the recency cap) is a legitimate
        # empty and must NOT crash the job — that false crash is exactly what
        # failed every King scrape pre-fix, when the cap dropped 100% of parcels.
        if is_parse_break(stats, len(records)):
            raise RuntimeError(
                f"King tax delinquent scanned {stats['total_rows']} rows but "
                f"produced {stats['aggregated_parcels']} candidate parcels "
                f"(overflow={stats['overflow']}) — likely a parse/gate bug or "
                f"source-format change"
            )
        if not records and stats["total_rows"] >= _CANARY_MIN_ROWS:
            _logger.warning(
                "King tax delinquent: %d rows scanned, %d candidate parcels, but 0 "
                "emitted — all removed by business filters (net-zero=%d, capped=%d, "
                "overflow=%d). Legitimate empty, not a parse error.",
                stats["total_rows"], stats["aggregated_parcels"],
                stats["net_zero_parcels"], stats["capped_out"], stats["overflow"],
            )

        if self.on_progress:
            self.on_progress(1, 1, len(records))
        return records

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass
