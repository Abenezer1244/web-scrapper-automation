"""Phase 4 — tax-delinquent view/export filters (amount owed + time delinquent).

Pure, DB-agnostic translation of a user-facing filter into Result column
predicates. This is a VIEW/EXPORT filter (option B): it narrows what the user
sees and exports, it does NOT change scraping or billing.

"Months delinquent" is derived from `delinquent_bill_year` at query time (King
property-tax bills issue ~Jan 1 of the bill year) so it never goes stale:
    months_delinquent(Y) = base - 12*Y,  base = today.year*12 + (today.month - 1)
which is monotonically decreasing in Y, so a months RANGE maps to a bill_year
range. Rows with NULL structured columns (every non-King-tax row) never satisfy
a `>=`/`<=` comparison, so they are correctly excluded whenever a filter is set.
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import or_

from src.db.models import Result

# Hard product cap: a tax-delinquent parcel is visible (and stored by future
# scrapes) ONLY if its OLDEST unpaid bill year is within this many months of
# today. Parcels whose delinquency reaches further back are hidden everywhere
# and dropped at ingestion. User decision 2026-06-16: "drop if oldest year >18mo"
# — Claude and Codex both flagged that this also drops parcels which are
# delinquent right now but carry old debt too; user confirmed the trade with
# full dissent on record (recency over volume).
#
# Year granularity: `delinquent_bill_year` is a YEAR (bills modeled ~Jan 1), so
# the cutoff is calendar-year-approximated — a Jan-2025 bill reads as ~17.5mo in
# mid-2026, just inside an 18-month window, and flips out as the year turns. The
# caller MUST freeze `today` for the whole request/job (use UTC, matching
# build_tax_conditions) so the cap and the optional months filter never drift.
DEFAULT_TAX_CAP_MONTHS = 18

# Bind-parameter name for the raw-SQL twin (tax_cap_sql). Callers bind this to
# tax_cap_min_year(today) on their hand-written queries.
TAX_CAP_BIND = "tax_cap_min_year"


def bill_year_bounds_for_months(
    min_months: int | None,
    max_months: int | None,
    today: date,
) -> tuple[int | None, int | None]:
    """Translate a months-delinquent range into (max_bill_year, min_bill_year).

    months_delinquent(Y) = base - 12*Y, base = today.year*12 + (today.month-1).
    - `min_months` (delinquent for AT LEAST N months) -> bill_year <= max_year
      (older bills are more delinquent), via floor.
    - `max_months` (AT MOST N months) -> bill_year >= min_year, via ceil.
    Each bound is None when its filter is unset.
    """
    base = today.year * 12 + (today.month - 1)
    max_year: int | None = None
    min_year: int | None = None
    if min_months is not None:
        # base - 12Y >= min_months  ->  Y <= (base - min_months)/12  (floor)
        max_year = (base - min_months) // 12
    if max_months is not None:
        # base - 12Y <= max_months  ->  Y >= (base - max_months)/12  (ceil)
        num = base - max_months
        min_year = -((-num) // 12)
    return (max_year, min_year)


def tax_cap_min_year(today: date) -> int:
    """Oldest `delinquent_bill_year` still visible under the 18-month cap.

    Reuses bill_year_bounds_for_months (the same math the optional months filter
    uses) so the hard cap and the user filter can never drift. `max_months` is
    always passed, so the returned min_year is never None.
    """
    _, min_year = bill_year_bounds_for_months(None, DEFAULT_TAX_CAP_MONTHS, today)
    assert min_year is not None  # max_months is always supplied above
    return min_year


def tax_cap_condition(today: date):
    """ORM predicate enforcing the 18-month cap on a Result query.

    SELF-SCOPING: rows with NULL `delinquent_bill_year` (every non-tax row) pass
    untouched, so this is safe to AND onto ANY Result query without first
    checking record_type. Tax rows survive only when their oldest unpaid year is
    within the window.
    """
    min_year = tax_cap_min_year(today)
    return or_(
        Result.delinquent_bill_year.is_(None),
        Result.delinquent_bill_year >= min_year,
    )


def tax_cap_sql(alias: str) -> str:
    """Raw-SQL twin of tax_cap_condition for the hand-written segments/batch
    queries. The caller MUST bind ``:tax_cap_min_year`` (= tax_cap_min_year(today)).

    Same self-scoping via IS NULL as the ORM clause, so non-tax rows pass.
    """
    col = f"{alias}.delinquent_bill_year"
    return f"({col} IS NULL OR {col} >= :{TAX_CAP_BIND})"


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
        max_year, min_year = bill_year_bounds_for_months(min_months, max_months, today)
        if max_year is not None:
            conditions.append(Result.delinquent_bill_year <= max_year)
        if min_year is not None:
            conditions.append(Result.delinquent_bill_year >= min_year)
    return conditions
