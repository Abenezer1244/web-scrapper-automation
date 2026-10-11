"""The retired cache endpoint stays gone, and the shared text-date parser stays total.

GET /scrapers/{id}/records served the shared county_records cache. It was retired in UX
3.13a: the frontend stopped calling it in 3.12 (its records page is now a redirect), the
cache is frozen, and the endpoint read that shared PII table without the quota, payment
or Starter-delay gates (SECURITY-AUDIT S3-21). The first test pins the retirement; it is
synchronous, needs no fixture and no database, and runs no app lifespan.

The SQL date parser tests below were written for that endpoint's ordering. The parser
(results_sort._text_date / _valid_date) also orders the Results page and places dates in
the Segments windows, so they stay. The junk values are real shapes from prod Pierce rows
(instrument numbers and scraped UI text in date_recorded): they must parse as "no date",
never fail a query. Evaluated by PostgreSQL, no mocks.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from main import app
from sqlalchemy import literal, select
from sqlalchemy import text as text_sql
from sqlalchemy.dialects import postgresql
from starlette.routing import Match

from src.api.results_sort import _text_date, _valid_date


def test_the_cached_records_endpoint_is_retired():
    """No route serves the path: FastAPI answers its default 404 before any dependency
    (no auth, no DB, no rate limit), and the OpenAPI contract no longer lists it."""
    path = f"/scrapers/{uuid.uuid4()}/records"
    response = TestClient(app).get(path)  # no `with`: no lifespan, no startup work
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "",
        "query_string": b"", "headers": [(b"host", b"test")], "server": ("test", 80),
        "client": ("127.0.0.1", 50000), "state": {}, "app": app,
    }
    assert [r for r in app.routes if r.matches(scope)[0] == Match.FULL] == []

    spec = app.openapi()
    assert "/scrapers/{config_id}/records" not in spec["paths"]
    assert not {"CachedRecordRow", "CachedResultsPage"} & set(spec["components"]["schemas"])


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
