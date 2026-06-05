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

from src.db.models import Result


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
