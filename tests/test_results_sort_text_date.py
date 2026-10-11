"""The SQL date parser and guard are total: every input yields a date or NULL, never an error.

results_sort._text_date parses free-text date_recorded inside PostgreSQL for the auction-date
fallback used by Results ordering and the Segments stand-in filter. results_sort._valid_date
guards _month_name_date and _iso_date, which feed Results ordering and the Segments filing-date
windows. The junk values are real shapes from prod Pierce rows (instrument numbers and scraped
UI text in date_recorded): they must parse as "no date", never fail a query. Evaluated by
PostgreSQL, no mocks. (Moved here unchanged in UX 3.13b.)
"""
import pytest
from sqlalchemy import literal, select
from sqlalchemy import text as text_sql
from sqlalchemy.dialects import postgresql

from src.api.results_sort import _text_date, _valid_date


@pytest.mark.parametrize(("text", "expected"), [
    ("3/20/2026", "2026-03-20"),
    (" 03/13/2026 ", "2026-03-13"),
    ("2/29/2028", "2028-02-29"),
    ("September 18, 2026", "2026-09-18"),
    ("Sept. 5 2026", "2026-09-05"),
    ("2/29/2027", None),
    ("13/1/2026", None),
    ("0/10/2026", None),
    ("4/31/2026", None),
    ("1/0/2026", None),
    ("12/31/0000", None),
    ("99/99/9999", None),
    ("200005310610", None),
    ("#ImageItem SelectInstrument #Boo", None),
    ("View", None),
    ("", None),
    (None, None),
])
async def test_text_date_parser_is_total(db, text: str | None, expected: str | None):
    """Every input yields a date or NULL. Evaluated by PostgreSQL, not Python."""
    value = (await db.execute(select(_text_date(literal(text))))).scalar_one()
    assert (value.isoformat() if value else None) == expected


@pytest.mark.parametrize(("year", "month", "day", "expected"), [
    (2027, 2, 29, None), (2026, 13, 1, None), (2026, 0, 5, None), (0, 1, 1, None),
    (2026, 4, 31, None), (2026, 1, 0, None), (2026, 1, 99, None), (99999, 1, 1, None),
    (2028, 2, 29, "2028-02-29"), (9999, 12, 31, "9999-12-31"),
])
async def test_date_guard_survives_inlined_constants(db, year, month, day, expected):
    """The guard with every value INLINED as a SQL constant, not bound. The planner
    may evaluate constant expressions in branches that never run, so this is the
    harshest form: an impossible date must still come out NULL, never an error."""
    stmt = select(_valid_date(literal(year), literal(month), literal(day)))
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert ":" not in sql  # really inlined, nothing left to bind
    value = (await db.execute(text_sql(sql))).scalar_one()
    assert (value.isoformat() if value else None) == expected
