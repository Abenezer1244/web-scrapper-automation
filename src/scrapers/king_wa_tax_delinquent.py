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

from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from src.api.middleware.security import add_scrape_domain
from src.scrapers.base_scraper import BridgeScraper, ScrapedRecord
from src.utils.logger import setup_logger
from src.utils.safe_http import safe_get

_logger = setup_logger("scraper.king_wa_tax_delinquent")

_API_URL = "https://data.kingcounty.gov/resource/dsv3-ct3e.json"
_HEADERS = {"User-Agent": "Mozilla/5.0 BridgeLeads/1.0"}
_SOURCE = "king_county_delinquent_taxes"

add_scrape_domain("data.kingcounty.gov")

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
    rows, *, start_year: int, effective_end_year: int
) -> tuple[list[ScrapedRecord], dict]:
    """Aggregate raw Socrata rows into one ScrapedRecord per delinquent parcel.

    Pure (no I/O) so it is unit-testable — ``rows`` is any iterable of row dicts
    (a live paginating generator in prod, a plain list in tests). Accumulates
    fully before emitting: a parcel's charge lines span API pages, so NOTHING is
    emitted mid-stream.

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
        "overflow": 0,
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

    records: list[ScrapedRecord] = []
    for parcel, entry in agg.items():
        # Floor at the PARCEL total (not per line) — preserves partial-payment /
        # credit math within the parcel before clamping.
        total_cents = entry["owed_cents"]
        if total_cents <= 0:
            continue  # net not-owed (fully paid / credit-offset) = not a lead
        amount = (Decimal(total_cents) / 100).quantize(Decimal("0.01"))
        if amount > _AMOUNT_MAX:
            stats["overflow"] += 1
            continue  # quarantine absurd values rather than truncate

        years_sorted = sorted(entry["years"])
        bill_year = years_sorted[0]  # oldest delinquent year (matches Snoho)

        rec = ScrapedRecord()
        rec.parcel_id = parcel
        rec.party_name = f"Tax Delinquent — ${amount:,.0f} owed (Parcel {parcel})"
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

    def __init__(self, record_type: str = "tax_delinquent"):
        super().__init__()

    def _iter_api_rows(self, where: str):
        """Yield rows across all Socrata pages — a parcel's lines may span pages."""
        offset = 0
        page_num = 0
        while True:
            params = {
                "$where": where,
                "$order": "account_number,bill_year",  # stable pagination
                "$limit": _PAGE_SIZE,
                "$offset": offset,
            }
            try:
                # S4: safe_http (SSRF defense-in-depth) — re-validates the fixed
                # HTTPS Socrata endpoint, disables ambient proxy, refuses
                # redirect-to-internal.
                resp = safe_get(_API_URL, params=params, headers=_HEADERS, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                # FAIL LOUD on a mid-pagination error — never silently truncate.
                # A partial parcel set would ship an incomplete lead list that the
                # zero-parcel canary can't catch. Raise so the job fails + retries.
                raise RuntimeError(
                    f"King tax delinquent: API page fetch failed at offset {offset} "
                    f"(page {page_num + 1}) — aborting to avoid a truncated result: "
                    f"{str(exc)[:120]}"
                ) from exc

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

        records, stats = aggregate_delinquent_rows(
            self._iter_api_rows(where),
            start_year=start_year,
            effective_end_year=effective_end,
        )

        _logger.info(
            "King WA tax delinquent complete — %d rows scanned, %d parcels emitted "
            "(%d malformed-acct skipped, %d abatement rows, %d unknown-type rows, "
            "%d overflow)",
            stats["total_rows"], len(records), stats["skipped_malformed_acct"],
            stats["abatement_rows"], stats["unknown_type_rows"], stats["overflow"],
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
        # Structural canary: a sizeable scan that yields zero parcels means the
        # gate/parse broke or the source changed shape — fail loud, don't ship empty.
        if stats["total_rows"] >= 100 and not records:
            raise RuntimeError(
                f"King tax delinquent scanned {stats['total_rows']} rows but produced "
                f"0 parcels — likely a parse/gate bug or source-format change"
            )

        if self.on_progress:
            self.on_progress(1, 1, len(records))
        return records

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass
