"""The recency cap exempts King (source-keyed) at query time — pure, no DB.

King's Socrata feed is a currently-unpaid delinquency roll, so its bill_year is
not a recency signal; King is exempt from the hard cap everywhere it's enforced
(user decision 2026-06-23). These tests lock that the ORM clause and the raw-SQL
twin both carry the source escape hatch and stay in sync from one constant.
"""
from datetime import date

from sqlalchemy.dialects import postgresql

from src.api.tax_filters import (
    TAX_CAP_EXEMPT_SOURCES,
    _exempt_sources_sql,
    tax_cap_condition,
    tax_cap_sql,
)

_TODAY = date(2026, 6, 23)


def _compiled(expr) -> str:
    return str(
        expr.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_king_in_exempt_sources():
    assert "king_county_delinquent_taxes" in TAX_CAP_EXEMPT_SOURCES


def test_exempt_sources_sql_is_quoted_list():
    sql = _exempt_sources_sql()
    assert "'king_county_delinquent_taxes'" in sql


def test_orm_condition_has_source_exemption_and_bill_year_bounds():
    sql = _compiled(tax_cap_condition(_TODAY))
    # self-scoping NULL pass + the recency lower bound + the King source escape hatch
    assert "delinquent_bill_year IS NULL" in sql
    assert "delinquent_bill_year >=" in sql
    assert "enrichment_data ->> 'source'" in sql
    assert "king_county_delinquent_taxes" in sql


def test_raw_sql_twin_matches_orm_shape():
    frag = tax_cap_sql("r")
    assert "r.delinquent_bill_year IS NULL" in frag
    assert "r.delinquent_bill_year >= :tax_cap_min_year" in frag
    assert "r.enrichment_data->>'source' IN ('king_county_delinquent_taxes')" in frag


def test_exempt_sql_rejects_unsafe_token(monkeypatch):
    # The inliner must refuse a non-lowercase-token source (injection guard).
    import src.api.tax_filters as tf

    monkeypatch.setattr(tf, "TAX_CAP_EXEMPT_SOURCES", frozenset({"bad'; DROP"}))
    try:
        tf._exempt_sources_sql()
    except AssertionError:
        return
    raise AssertionError("expected AssertionError on unsafe source token")
