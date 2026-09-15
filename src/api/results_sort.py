"""Server-side ordering for the per-job Results page (GET /jobs/{id}/results).

The page used to order by ``Result.created_at``. Every row of a job is inserted in
one transaction, so they all share one ``created_at`` (a 9-row job had 1 distinct
value, an 18,214-row job had 19). That order was no order at all: Postgres returned
the ties in whatever sequence the heap produced, and OFFSET paging over those ties
was not even guaranteed stable between pages.

The sort key is the value the table's first column actually shows:
  - tax_delinquent jobs show the oldest delinquent tax YEAR (county tax data has no
    per-record event date; any ``date_recorded`` there is a synthetic 01/01/YYYY the
    UI hides), so they sort by ``delinquent_bill_year``;
  - every other record type shows ``date_recorded``.

``date_recorded`` is free text and is NOT rewritten here: it feeds ``dedup_hash`` and
``source_fingerprint``, so normalizing stored values would stop a re-scraped lead
matching the copy that was already delivered. Sorting reads a parsed DATE instead:
``date_recorded_parsed`` (DB-generated, M/D/YYYY only) and, when that is NULL, the
"Month D, YYYY" form the Snohomish pre_foreclosure scraper writes when a notice has
no mailing date. Rows with no parseable date sort after every dated row in both
directions; nothing is fabricated for them.

Ordering happens in the same statement as OFFSET/LIMIT, so page 1 holds the first
rows of the WHOLE filtered set. ``Result.id`` breaks ties: it is unique and never
updated, so enrichment rewriting names or addresses cannot move a row.
"""
from typing import Literal

from sqlalchemy import Integer, Text, case, cast, func, or_
from sqlalchemy.dialects.postgresql import ARRAY

from src.db.models import Result

ResultsSort = Literal["date_desc", "date_asc"]
DEFAULT_RESULTS_SORT: ResultsSort = "date_desc"

# "September 18, 2026", "Sep 18 2026", "Sept. 18, 2026". Captures word, day, year.
_MONTH_NAME_DATE = r"^\s*([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})\s*$"
_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_MONTH_NUMBERS = {
    **{name: i for i, name in enumerate(_MONTHS, start=1)},
    **{name[:3]: i for i, name in enumerate(_MONTHS, start=1)},
    "sept": 9,
}


def _month_name_date(column):
    """DATE from a "Month D, YYYY" string, or NULL. Never raises.

    ``make_date`` errors on an impossible date, and one bad row would fail the whole
    page, so every value it receives is proven valid first. The guards are NESTED
    CASEs because Postgres guarantees a CASE evaluates its branch only when the
    condition holds, and does not guarantee the evaluation order of AND/OR.
    """
    parts = func.regexp_match(column, _MONTH_NAME_DATE, type_=ARRAY(Text))
    month = case(_MONTH_NUMBERS, value=func.lower(parts[1]), else_=None)
    day = cast(parts[2], Integer)
    year = cast(parts[3], Integer)
    # Day 31 of a 30-day month rolls into the next month; a real day stays put.
    stays_in_month = (
        func.date_part("month", func.make_date(year, month, 1) + (day - 1)) == month
    )
    return case(
        # month IS NULL covers "no regex match" (all parts NULL) and unknown words.
        # make_date rejects year 0, which \d{4} would otherwise let through.
        (or_(month.is_(None), year < 1), None),
        else_=case(
            (day.between(1, 31), case((stays_in_month, func.make_date(year, month, day)))),
        ),
    )


def results_order_by(record_type: str | None, sort: ResultsSort) -> list:
    """ORDER BY clauses for the Results page, given the job's record type."""
    if record_type == "tax_delinquent":
        key = Result.delinquent_bill_year
    else:
        key = func.coalesce(Result.date_recorded_parsed, _month_name_date(Result.date_recorded))
    ordered = key.asc() if sort == "date_asc" else key.desc()
    # Explicit in both directions: Postgres puts NULLs FIRST on DESC by default,
    # which would open the page with the undated rows.
    return [ordered.nulls_last(), Result.id.asc()]
