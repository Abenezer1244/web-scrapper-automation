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
  - every other record type shows ``date_recorded``, except where it is the auction
    date a scraper stood in for a missing notice date (trustee_sale, Snohomish
    pre_foreclosure). The page shows that as blank, so it sorts with the undated rows
    (``auction_date_fallback_condition``, owner decision 2026-09-15).

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

from sqlalchemy import JSON, Integer, Text, and_, case, cast, func, literal_column, type_coerce
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import ARRAY

from src.db.models import CountyRecord, Result

ResultsSort = Literal["date_desc", "date_asc"]
DEFAULT_RESULTS_SORT: ResultsSort = "date_desc"

# "September 18, 2026", "Sep 18 2026", "Sept. 18, 2026". Captures word, day, year.
_MONTH_NAME_DATE = "^([A-Za-z]+)[.]? +([0-9]{1,2}),? +([0-9]{4})$"
_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_MONTH_NUMBERS = {
    **{name: i for i, name in enumerate(_MONTHS, start=1)},
    **{name[:3]: i for i, name in enumerate(_MONTHS, start=1)},
    "sept": 9,
}


# "3/20/2026", "03/13/2026". Captures month, day, year.
_NUMERIC_DATE = "^([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})$"


def _valid_date(year, month, day):
    """The calendar date year-month-day when it is real, else NULL. Never raises.

    ``make_date`` errors on an impossible date, and one bad row would fail the whole
    page. Guarding it with CASE is not enough on its own: the planner may evaluate a
    constant expression inside a branch that never runs (Codex). So ``make_date``
    only ever receives values clamped into range (month 1-12, day 1, year 1-9999),
    whatever the input, and the day is added with date arithmetic, which cannot
    raise. Out-of-range or NULL parts (no regex match, unknown month word) and a day
    that rolls into the next month (April 31) all come out NULL.
    """
    safe_year = case((year.between(1, 9999), year), else_=2000)
    safe_month = case((month.between(1, 12), month), else_=1)
    safe_day = case((day.between(1, 31), day), else_=1)
    candidate = func.make_date(safe_year, safe_month, 1) + (safe_day - 1)
    is_real = and_(
        year.between(1, 9999),
        month.between(1, 12),
        day.between(1, 31),
        # A real day stays in its month; day 31 of a 30-day month does not.
        func.date_part("month", candidate) == month,
    )
    return case((is_real, candidate), else_=None)


def _month_name_date(column):
    """DATE from a "Month D, YYYY" string, or NULL. Never raises."""
    parts = func.regexp_match(func.btrim(column), _MONTH_NAME_DATE, type_=ARRAY(Text))
    month = case(_MONTH_NUMBERS, value=func.lower(parts[1]), else_=None)
    return _valid_date(cast(parts[3], Integer), month, cast(parts[2], Integer))


def _numeric_date(column):
    """DATE from an "M/D/YYYY" string, or NULL. Never raises.

    Same text ``results.date_recorded_parsed`` accepts. Results keeps reading that
    stored column; this is for tables that have no parsed date.
    """
    parts = func.regexp_match(func.btrim(column), _NUMERIC_DATE, type_=ARRAY(Text))
    return _valid_date(
        cast(parts[3], Integer), cast(parts[1], Integer), cast(parts[2], Integer)
    )


def _text_date(column):
    """DATE from either date form a county source writes, or NULL. Never raises."""
    return func.coalesce(_numeric_date(column), _month_name_date(column))


# "2026-09-18". Only the trustee_sale scraper's recorded auction date uses it.
_ISO_DATE = "^([0-9]{4})-([0-9]{2})-([0-9]{2})$"


def _iso_date(column):
    """DATE from a "YYYY-MM-DD" string, or NULL. Never raises."""
    parts = func.regexp_match(func.btrim(column), _ISO_DATE, type_=ARRAY(Text))
    return _valid_date(
        cast(parts[1], Integer), cast(parts[2], Integer), cast(parts[3], Integer)
    )


def _auction_date_fallback(date_recorded, enrichment_data):
    """SQL twin of ``src.utils.source_dates.is_auction_date_fallback``.

    True when date_recorded is the auction date its scraper stood in for a missing
    notice date. Compared with the auction date the scraper RECORDED in
    enrichment_data, never with results.auction_date, which the NTS matcher moves on a
    postponement. NULL (not a stand-in) whenever either side is missing or unreadable.
    Same grammar as the Python rule: date_recorded is parsed here, not read from
    date_recorded_parsed, which only knows M/D/YYYY.
    """
    origin = func.coalesce(
        _text_date(enrichment_data["auction_date"].as_string()),
        _iso_date(enrichment_data[("nts_source", "auction_date")].as_string()),
    )
    # enrichment_data is json, not jsonb: every -> re-parses the whole document, and
    # the expression above reads it dozens of times. Both origin keys are named
    # auction_date, so a row whose stored text never mentions it cannot be a stand-in;
    # json->text is the stored text, no parse. Measured on prod (read-only, 92k rows of
    # one account): 13.4 s for the bare rule over every row, 148 ms with this check
    # (58-348 ms with no rule at all), and the same 83 stand-ins either way.
    mentions_auction_date = func.strpos(cast(enrichment_data, Text), "auction_date") > 0
    return case((mentions_auction_date, _text_date(date_recorded) == origin), else_=None)


def auction_date_fallback_condition():
    """The stand-in rule on ``results`` for ORM queries (NULL means "not a stand-in")."""
    return _auction_date_fallback(Result.date_recorded, Result.enrichment_data)


def auction_date_fallback_sql(alias: str) -> str:
    """The same rule as a raw SQL boolean on ``<alias>.date_recorded`` and
    ``<alias>.enrichment_data``, for the hand-written text() queries (Lists).

    Rendered from the expression above with every literal inlined, so there is one
    definition. That is only safe because no pattern contains a backslash (inlining
    doubles them) and nothing renders a colon (text() would read it as a bind), so
    both are refused rather than assumed. The result may contain braces: a caller that
    runs it through str.format must escape them.
    """
    if not alias.isidentifier():
        raise ValueError(f"not a SQL alias: {alias!r}")
    expr = _auction_date_fallback(
        literal_column(f"{alias}.date_recorded"),
        type_coerce(literal_column(f"{alias}.enrichment_data"), JSON),
    )
    sql = str(expr.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    if "\\" in sql or ":" in sql:
        raise RuntimeError("stand-in SQL must not contain a backslash or a colon")
    return f"COALESCE(({sql}), FALSE)"


def results_order_by(record_type: str | None, sort: ResultsSort) -> list:
    """ORDER BY clauses for the Results page, given the job's record type."""
    if record_type == "tax_delinquent":
        key = Result.delinquent_bill_year
    else:
        # An auction-date stand-in is not a notice date: it sorts with the undated rows.
        key = case(
            (auction_date_fallback_condition(), None),
            else_=func.coalesce(
                Result.date_recorded_parsed, _month_name_date(Result.date_recorded)
            ),
        )
    ordered = key.asc() if sort == "date_asc" else key.desc()
    # Explicit in both directions: Postgres puts NULLs FIRST on DESC by default,
    # which would open the page with the undated rows.
    return [ordered.nulls_last(), Result.id.asc()]


def cached_records_order_by() -> list:
    """ORDER BY clauses for the cached records page (GET /scrapers/{id}/records).

    The cache is refreshed in batches that share one scraped_at (Benton: 2,574 rows,
    one timestamp), so scraped_at alone left each batch in heap order. It stays the
    primary key, which the "new since you last looked" feed depends on; within a batch
    rows run newest date first, undated last, and id makes the order total.
    county_records has no parsed date column, so the text is parsed here.
    """
    return [
        CountyRecord.scraped_at.desc(),
        _text_date(CountyRecord.date_recorded).desc().nulls_last(),
        CountyRecord.id.asc(),
    ]
