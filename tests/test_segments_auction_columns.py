"""The Lists CSV must actually carry every column it prints a header for.

`OVERLAP_LEAD_COLUMNS` gained `auction_date` / `days_to_auction` / `default_amount`
specifically because "a pre_foreclosure or trustee_sale lead delivered through a batch
or a SEGMENT lost its auction date and amount owed even though the per-job CSV carried
both" (src/utils/lead_export.py:775-781). `batch_export.py` was fixed alongside that
comment; `segments.py` was not, so every Lists export printed auction headers over
permanently empty columns - and the same omission silently blanked `doc_type`, `heirs`,
`legal_description`, `delinquent_amount` and `delinquent_bill_year`.

`build_overlap_export_row` reads each value with `getattr(row, name, None)`, so a column
absent from the SELECT is **indistinguishable from a NULL one**: it renders `""` and
nothing anywhere complains. There is no runtime signal for this class of bug, which is
why it survived a targeted fix to the sibling exporter. These tests assert the
projection itself, and derive what to expect from the CSV contract rather than from a
hand-written list, so adding a column to `OVERLAP_LEAD_COLUMNS` without wiring it into
the queries fails here instead of shipping blank.
"""
import pytest

from src.api.routes.segments import (
    _INTERSECTION_DATED_SQL,
    _INTERSECTION_SQL,
    _UNION_SQL,
)
from src.utils.lead_export import OVERLAP_LEAD_COLUMNS

# CSV column -> the `results` column its value is read from. Only entries whose value
# comes STRAIGHT off a Result column belong here; everything else in
# OVERLAP_LEAD_COLUMNS is derived, parsed, or supplied by the overlap aggregate:
#   overlap/lists_count/lists/counties  - the overlap dict, not the row
#   first_name/last_name                - parsed from party_name
#   property_*/mailing_*                - parsed from property_address/mailing_address
#   phone_2/3, email_2/3                - unpacked from phones/emails
#   days_to_auction                     - derived from auction_date at render time
#   lead_subtype                        - already projected as a scalar alias
_CSV_COLUMN_TO_RESULT_COLUMN = {
    "filed_date": "date_recorded",
    "party_name": "party_name",
    "parcel_id": "parcel_id",
    "property_address": "property_address",
    "mailing_address": "mailing_address",
    "phone": "phone",
    "phone_type": "phone_type",
    "email": "email",
    "doc_type": "doc_type",
    "heirs": "heirs",
    "legal_description": "legal_description",
    "delinquent_amount": "delinquent_amount",
    "delinquent_bill_year": "delinquent_bill_year",
    "auction_date": "auction_date",
    "default_amount": "default_amount",
}

# Derived at render time from a column that IS selected; selecting it would imply a
# stored column that does not exist.
_DERIVED_NEVER_SELECTED = ("days_to_auction",)

ALL_SEGMENT_SQL = {
    "intersection": _INTERSECTION_SQL,
    "union": _UNION_SQL,
    "intersection_dated": _INTERSECTION_DATED_SQL,
}

REQUIRED_RESULT_COLUMNS = sorted(set(_CSV_COLUMN_TO_RESULT_COLUMN.values()))


def _sql(template: str) -> str:
    return template.format(county_clause="")


def _sql_without_comments(template: str) -> str:
    """The SQL with `-- ...` comment text removed.

    An assertion about what a query SELECTs must not be satisfiable - or breakable - by
    prose: the comment explaining these columns names several of them, including
    `days_to_auction`, which would otherwise trip the derived-column check below.
    """
    return "\n".join(line.split("--", 1)[0] for line in _sql(template).splitlines())


def test_the_mapping_covers_the_csv_contract():
    """Guards the guard: if a column is added to OVERLAP_LEAD_COLUMNS, it must be
    classified here as row-backed or derived, not silently ignored."""
    unclassified = set(OVERLAP_LEAD_COLUMNS) - set(_CSV_COLUMN_TO_RESULT_COLUMN)
    # Everything left over must be a known derived/parsed/aggregate column.
    known_non_row = {
        "overlap", "lists_count", "lists", "counties",
        "first_name", "last_name",
        "phone_2", "phone_3", "email_2", "email_3",
        "property_street", "property_city", "property_state", "property_zip",
        "mailing_street", "mailing_city", "mailing_state", "mailing_zip",
        "lead_subtype", *_DERIVED_NEVER_SELECTED,
    }
    assert unclassified == known_non_row, (
        "OVERLAP_LEAD_COLUMNS changed. Classify the new column as row-backed "
        "(add it to _CSV_COLUMN_TO_RESULT_COLUMN and to the segment SELECTs) or "
        f"derived (add it to known_non_row). Unclassified: {unclassified ^ known_non_row}"
    )


@pytest.mark.parametrize("name", sorted(ALL_SEGMENT_SQL))
@pytest.mark.parametrize("column", REQUIRED_RESULT_COLUMNS)
def test_every_segment_query_projects_every_promised_column(name, column):
    """Both the inner per-result SELECT and the final projection must carry it.

    Two occurrences, not one: the inner SELECT feeds a CTE and the outer SELECT projects
    from the ranked CTE. Losing either one blanks the column in the CSV.
    """
    sql = _sql_without_comments(ALL_SEGMENT_SQL[name])
    assert f"r.{column}" in sql, f"{name}: inner SELECT drops {column}"
    assert f"rk.{column}" in sql, f"{name}: final projection drops {column}"


@pytest.mark.parametrize("column", _DERIVED_NEVER_SELECTED)
def test_derived_columns_are_not_selected(column):
    for name, template in ALL_SEGMENT_SQL.items():
        assert column not in _sql_without_comments(template), (
            f"{name}: {column} is derived at render time, not a stored column"
        )


def test_the_csv_contract_still_promises_the_auction_columns():
    """If someone removes these from the contract the tests above quietly weaken
    rather than fail. Pin the promise itself."""
    for column in ("auction_date", "days_to_auction", "default_amount"):
        assert column in OVERLAP_LEAD_COLUMNS
