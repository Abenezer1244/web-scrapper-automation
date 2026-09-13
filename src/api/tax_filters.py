"""Phase 4 — tax-delinquent view/export filters (amount owed + time delinquent).

Pure, DB-agnostic translation of a user-facing filter into Result column
predicates. This is a VIEW/EXPORT filter (option B): it narrows what the user
sees and exports, it does NOT change scraping or billing.

"Months delinquent" is derived from `delinquent_bill_year` at query time, counted
from May 1 of the bill year (WA first-half delinquency, RCW 84.56.020), so it
never goes stale:
    months_delinquent(Y) = max(0, anchor - 12*Y)
    anchor = today.year*12 + (today.month - 1) - (WA_FIRST_DELINQUENT_MONTH - 1)
which is non-increasing in Y, so a months RANGE maps to a bill_year range. Rows
with a NULL `delinquent_bill_year` (every non-tax row) are excluded whenever a
months filter is set.
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import or_

from src.db.models import Result
from src.utils.lead_signals import delinquency_anchor

# Hard product cap: a tax-delinquent parcel is visible (and stored by future
# scrapes) ONLY if its OLDEST unpaid bill year is within this many months of
# today. Parcels whose delinquency reaches further back are hidden everywhere
# and dropped at ingestion. User decision 2026-06-16: "drop if oldest year >18mo"
# — Claude and Codex both flagged that this also drops parcels which are
# delinquent right now but carry old debt too; user confirmed the trade with
# full dissent on record (recency over volume).
#
# Year granularity: `delinquent_bill_year` is a YEAR, so the cutoff is
# approximate; see tax_cap_min_year for how last year is kept visible. The
# caller MUST freeze `today` for the whole request/job (use UTC, matching
# build_tax_conditions) so the cap and the optional months filter never drift.
DEFAULT_TAX_CAP_MONTHS = 18

# Sources EXEMPT from the hard recency cap (user decision 2026-06-23).
#
# The cap assumes `bill_year` is a proxy for "how long delinquent" — true for a
# FULL tax roll (Snohomish), where you infer delinquency from bill_year vs the
# as-of year. It is FALSE for King's Socrata feed, which publishes ONLY the
# currently-unpaid receivables: every row is delinquent RIGHT NOW and `bill_year`
# is just the levy year, not a delinquency-age signal. King's open-data feed also
# lags ~1.5 years (its newest bill_year today is 2024, which the 18-month cap
# would read as ~29 months old), so a calendar cap drops 100% of King parcels and
# the scrape returns zero (and the canary used to crash on it). King is therefore
# exempt from the hard cap EVERYWHERE the cap is enforced (ingestion + view +
# export + dialer). This is the EXACT `enrichment_data["source"]` the King scraper
# stamps (an internally-assigned constant, never copied from scraped payload, so
# it stays a trust boundary). NOT applied to the user's optional months filter
# (build_tax_conditions): an explicit "delinquent < N months" request still
# excludes King's old bill years — only the standing safety cap is exempted.
#
# This is the SAME registry concept as _TRUSTED_TAX_SOURCES in
# workers/tasks_helpers/dedup.py: a county is exempted by adding its exact source
# string here AFTER confirming its feed has King-like delinquency-roll semantics.
TAX_CAP_EXEMPT_SOURCES = frozenset({
    "king_county_delinquent_taxes",  # King — Socrata delinquency roll (see above)
})

# Bind-parameter name for the raw-SQL twin (tax_cap_sql). Callers bind this to
# tax_cap_min_year(today) on their hand-written queries.
TAX_CAP_BIND = "tax_cap_min_year"


def _exempt_sources_sql() -> str:
    """Render TAX_CAP_EXEMPT_SOURCES as a SQL `IN (...)` value list.

    The values are code-owned constants, never user input. Each is asserted to be
    a simple lowercase token (``[a-z0-9_]``) so inlining it into the raw-SQL twin
    (tax_cap_sql) carries no injection risk and the ORM/raw clauses stay in sync
    from one source of truth.
    """
    for s in TAX_CAP_EXEMPT_SOURCES:
        assert s and all(c.islower() or c.isdigit() or c == "_" for c in s), (
            f"TAX_CAP_EXEMPT_SOURCES value {s!r} is not a safe lowercase token"
        )
    return ", ".join(f"'{s}'" for s in sorted(TAX_CAP_EXEMPT_SOURCES))


def bill_year_bounds_for_months(
    min_months: int | None,
    max_months: int | None,
    today: date,
) -> tuple[int | None, int | None]:
    """Translate a months-delinquent range into (max_bill_year, min_bill_year).

    months_delinquent(Y) = max(0, anchor - 12*Y) (see src/utils/lead_signals.py).
    - `min_months` (delinquent for AT LEAST N months) -> bill_year <= max_year
      (older bills are more delinquent), via floor. `min_months <= 0` gives NO
      bound: every clamped value is >= 0, so every tax row qualifies.
    - `max_months` (AT MOST N months, N >= 0) -> bill_year >= min_year, via ceil.
      The clamp cannot matter here because N >= 0.
    Each bound is None when it does not constrain. Callers that need "tax rows
    only" must add that themselves (build_tax_conditions does).
    """
    anchor = delinquency_anchor(today)
    max_year: int | None = None
    min_year: int | None = None
    if min_months is not None and min_months > 0:
        # anchor - 12Y >= min_months  ->  Y <= (anchor - min_months)/12  (floor)
        max_year = (anchor - min_months) // 12
    if max_months is not None:
        # anchor - 12Y <= max_months  ->  Y >= (anchor - max_months)/12  (ceil)
        num = anchor - max_months
        min_year = -((-num) // 12)
    return (max_year, min_year)


def tax_cap_min_year(today: date) -> int:
    """Oldest `delinquent_bill_year` still visible under the 18-month cap.

    Reuses bill_year_bounds_for_months (the same math the optional months filter
    uses), then FLOORS the result at last year: the most recent completed tax year
    is never capped out.

    Why the floor: the source only has a tax YEAR, and a full tax roll can only
    call a year delinquent once it is a PRIOR year (Snohomish excludes the current
    year). Counted from January 1, the minimum year became the current year from
    August, capping out every Snohomish parcel from Aug 1 to Dec 31. Counted from
    May 1 it still becomes the current year in December. With year-only data, "oldest
    unpaid year is last year" is the closest honest reading of 18 months: last
    year's first half went delinquent on May 1 (RCW 84.56.020), so it is at most
    19 months old on December 31. Older years stay capped as before.
    """
    _, min_year = bill_year_bounds_for_months(None, DEFAULT_TAX_CAP_MONTHS, today)
    assert min_year is not None  # max_months is always supplied above
    return min(min_year, today.year - 1)


def tax_cap_condition(today: date):
    """ORM predicate enforcing the recency cap on a Result query.

    SELF-SCOPING: rows with NULL `delinquent_bill_year` (every non-tax row, plus
    every tax county not in _TRUSTED_TAX_SOURCES) pass untouched, so this is safe
    to AND onto ANY Result query without first checking record_type. Capped tax
    rows survive only when their oldest unpaid year is within the window — EXCEPT
    rows whose source is in TAX_CAP_EXEMPT_SOURCES (King), which always pass (see
    the constant's docstring for why King's bill_year is not a recency signal).
    """
    min_year = tax_cap_min_year(today)
    return or_(
        Result.delinquent_bill_year.is_(None),
        Result.delinquent_bill_year >= min_year,
        Result.enrichment_data["source"].as_string().in_(TAX_CAP_EXEMPT_SOURCES),
    )


def tax_cap_sql(alias: str) -> str:
    """Raw-SQL twin of tax_cap_condition for the hand-written segments/batch
    queries. The caller MUST bind ``:tax_cap_min_year`` (= tax_cap_min_year(today)).

    Same self-scoping via IS NULL as the ORM clause, so non-tax rows pass; and the
    same TAX_CAP_EXEMPT_SOURCES escape hatch (King), via the JSON `source` field,
    so the two clauses can never drift.
    """
    col = f"{alias}.delinquent_bill_year"
    exempt = _exempt_sources_sql()
    return (
        f"({col} IS NULL OR {col} >= :{TAX_CAP_BIND} "
        f"OR {alias}.enrichment_data->>'source' IN ({exempt}))"
    )


def build_tax_conditions(
    min_amount: Decimal | float | None,
    max_amount: Decimal | float | None,
    min_months: int | None,
    max_months: int | None,
    today: date,
) -> list:
    """Return SQLAlchemy predicates on Result for the active tax filters.

    Empty list when no filter is set (the caller's query is unchanged). A set
    amount/months filter implicitly excludes NULL structured rows because NULL
    never satisfies a comparison — exactly the intended "non-tax rows drop out".
    """
    conditions: list = []
    if min_amount is not None:
        conditions.append(Result.delinquent_amount >= min_amount)
    if max_amount is not None:
        conditions.append(Result.delinquent_amount <= max_amount)
    if min_months is not None or max_months is not None:
        # Explicit, not implied by a comparison: min_months=0 adds no year bound,
        # and non-tax rows must still drop out of a months-filtered view.
        conditions.append(Result.delinquent_bill_year.is_not(None))
        max_year, min_year = bill_year_bounds_for_months(min_months, max_months, today)
        if max_year is not None:
            conditions.append(Result.delinquent_bill_year <= max_year)
        if min_year is not None:
            conditions.append(Result.delinquent_bill_year >= min_year)
    return conditions
