"""The Lists CSV must actually carry the auction columns it prints headers for.

`OVERLAP_LEAD_COLUMNS` gained `auction_date` / `days_to_auction` / `default_amount`
specifically because "a pre_foreclosure or trustee_sale lead delivered through a batch
or a SEGMENT lost its auction date and amount owed even though the per-job CSV carried
both" (src/utils/lead_export.py:775-781). `batch_export.py` was fixed alongside that
comment; `segments.py` was not, so every Lists export has been printing three auction
headers over three permanently empty columns.

`build_overlap_export_row` reads the value off the row with `getattr(row, name, None)`,
so a column absent from the SELECT is indistinguishable from a NULL one: it renders as
"" and nothing anywhere complains. These assert the projection itself.
"""
import pytest

from src.api.routes.segments import (
    _INTERSECTION_DATED_SQL,
    _INTERSECTION_SQL,
    _UNION_SQL,
)
from src.utils.lead_export import OVERLAP_LEAD_COLUMNS

# The three the combined/overlap CSV promises. `trustee` / `ts_number` are
# deliberately NOT in OVERLAP_LEAD_COLUMNS (a combined export mixes record types),
# so they are correctly absent here too.
AUCTION_COLUMNS = ("auction_date", "default_amount")

ALL_SEGMENT_SQL = {
    "intersection": _INTERSECTION_SQL,
    "union": _UNION_SQL,
    "intersection_dated": _INTERSECTION_DATED_SQL,
}


def _sql(template: str) -> str:
    return template.format(county_clause="")


def _sql_without_comments(template: str) -> str:
    """The SQL with `-- ...` comment text removed.

    An assertion about what a query SELECTs must not be satisfiable (or breakable)
    by prose: the comment explaining the auction columns names `days_to_auction`,
    which would otherwise trip the derived-column check below.
    """
    return "\n".join(
        line.split("--", 1)[0] for line in _sql(template).splitlines()
    )


# Parametrize over the NAMES, never the templates: a template as a parametrize
# value puts the entire generated SQL into the test id (571KB of failure output).
@pytest.mark.parametrize("name", sorted(ALL_SEGMENT_SQL))
@pytest.mark.parametrize("column", AUCTION_COLUMNS)
def test_every_segment_query_selects_the_auction_columns(name, column):
    """Both the inner per-result SELECT and the final projection must carry it.

    Two occurrences, not one: the inner SELECT feeds a CTE and the outer SELECT
    projects from the ranked CTE. Losing either one blanks the column.
    """
    sql = _sql_without_comments(ALL_SEGMENT_SQL[name])
    assert f"r.{column}" in sql, f"{name}: inner SELECT drops {column}"
    assert f"rk.{column}" in sql, f"{name}: final projection drops {column}"


def test_the_csv_contract_still_promises_these_columns():
    """If someone removes them from the CSV contract, the tests above become
    meaningless rather than failing. Pin the promise too."""
    for column in (*AUCTION_COLUMNS, "days_to_auction"):
        assert column in OVERLAP_LEAD_COLUMNS


def test_days_to_auction_is_derived_not_selected():
    """`days_to_auction` is computed at render time from `auction_date`
    (lead_signals.days_to_auction), so it must NOT appear in the SQL - selecting it
    would imply a stored column that does not exist."""
    for template in ALL_SEGMENT_SQL.values():
        assert "days_to_auction" not in _sql_without_comments(template)
