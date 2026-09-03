"""Lead actionability rule (src/api/lead_actionability.py) — pure, no DB.

Owner decision 2026-09-02: no property address AND no mailing address = not a
lead (kept in the table, but not shown, exported, counted or billed). The three
spellings of the rule (ORM, raw SQL, Python) must agree.
"""
import re
from types import SimpleNamespace

import pytest

from src.api.lead_actionability import (
    ADDRESS_PLACEHOLDER,
    DELIVERY_EXCLUDED_KEY,
    OVER_QUOTA,
    actionable_condition,
    actionable_sql,
    address_actionable_sql,
    is_actionable,
)

CASES = [
    # (property_address, mailing_address, actionable)
    ("22212 75TH STREET CT E", "22212 75TH STREET CT E, BUCKLEY, WA, 98321", True),
    ("3020 112TH AVE E", None, True),               # property only → mailable/visitable
    (None, "PO BOX 30, VAUGHN, WA, 98394-0030", True),  # mailing only → mailable
    (None, None, False),                              # Test 1: BERNATH / JOHNSON / …
    ("", "", False),
    ("   ", None, False),
    (ADDRESS_PLACEHOLDER, None, False),               # placeholder is not an address
    (ADDRESS_PLACEHOLDER, "5311 108TH AVENUE CT E, PUYALLUP, WA", True),
    # enrichment/parcel.py's failure return writes the placeholder into BOTH
    # columns. Rejecting it on only the property side let the row pass through
    # the mailing side and be listed, exported, counted AND BILLED with no
    # address anywhere (Codex, 2026-09-03).
    (None, ADDRESS_PLACEHOLDER, False),
    (ADDRESS_PLACEHOLDER, ADDRESS_PLACEHOLDER, False),
    (ADDRESS_PLACEHOLDER, f"  {ADDRESS_PLACEHOLDER}  ", False),   # btrim'd both sides
]


@pytest.mark.parametrize("prop, mail, expected", CASES)
def test_python_twin(prop, mail, expected):
    assert is_actionable(SimpleNamespace(property_address=prop, mailing_address=mail)) is expected
    assert is_actionable({"property_address": prop, "mailing_address": mail}) is expected


def test_parcel_alone_is_not_actionable():
    # BAKKE (Test 1): parcel 0121228036 but no address anywhere → not mailable.
    row = SimpleNamespace(parcel_id="0121228036", property_address=None, mailing_address=None)
    assert is_actionable(row) is False


def _sql_eval(sql: str, prop, mail) -> bool:
    """Evaluate the raw-SQL twin with PostgreSQL semantics: NULL fails IS NOT NULL,
    and btrim() strips surrounding whitespace before the <> comparisons."""
    def nn(v):  # IS NOT NULL
        return v is not None
    prop_ok = nn(prop) and prop.strip() != "" and prop.strip() != ADDRESS_PLACEHOLDER
    mail_ok = nn(mail) and mail.strip() != "" and mail.strip() != ADDRESS_PLACEHOLDER
    # Structural check that the SQL really encodes those comparisons, trimmed.
    # The placeholder is rejected on BOTH columns, so each side contributes
    # three mentions and two btrim()s.
    assert sql.count("property_address") == 3 and sql.count("mailing_address") == 3
    assert sql.count("btrim(") == 4
    assert sql.count(ADDRESS_PLACEHOLDER) == 2
    return prop_ok or mail_ok


@pytest.mark.parametrize("prop, mail, expected", CASES)
def test_sql_twin_agrees(prop, mail, expected):
    # All three spellings agree, including whitespace-only values (Codex).
    assert _sql_eval(actionable_sql("r"), prop, mail) is expected


def test_sql_twin_is_alias_scoped_and_literal_only():
    sql = actionable_sql("r")
    assert re.search(r"\br\.property_address\b", sql) and re.search(r"\br\.mailing_address\b", sql)
    assert ":" not in sql  # no binds — only the fixed placeholder literal
    assert ADDRESS_PLACEHOLDER in sql


def test_orm_condition_compiles_to_the_same_rule():
    compiled = str(actionable_condition().compile(compile_kwargs={"literal_binds": True}))
    assert "results.property_address IS NOT NULL" in compiled
    assert "results.mailing_address IS NOT NULL" in compiled
    assert ADDRESS_PLACEHOLDER in compiled
    assert " OR " in compiled


class TestQuotaExclusion:
    """The plan cap marks rows past the quota in enrichment_data. The standing
    rule must then hide them from display, export, counting AND billing at once,
    so those four can never disagree (Codex ruling, 2026-09-03)."""

    def _row(self, **kw):
        base = {"property_address": "123 MAIN ST, TACOMA, WA 98401",
                "mailing_address": None, "enrichment_data": None}
        base.update(kw)
        return base

    def test_marked_row_is_not_actionable_despite_a_real_address(self):
        row = self._row(enrichment_data={DELIVERY_EXCLUDED_KEY: OVER_QUOTA})
        assert is_actionable(row) is False
        assert is_actionable(SimpleNamespace(**row)) is False

    def test_unmarked_row_with_other_enrichment_keys_is_unaffected(self):
        row = self._row(enrichment_data={"assessed_value": 400000,
                                         "lead_subtype": "probate"})
        assert is_actionable(row) is True

    def test_a_different_exclusion_reason_does_not_quarantine(self):
        # Only the quota reason is a delivery exclusion today; an unknown value
        # must not silently start hiding rows.
        row = self._row(enrichment_data={DELIVERY_EXCLUDED_KEY: "something_else"})
        assert is_actionable(row) is True

    def test_null_enrichment_data_is_actionable(self):
        assert is_actionable(self._row(enrichment_data=None)) is True

    def test_marked_row_with_no_address_stays_unactionable(self):
        row = self._row(property_address=None,
                        enrichment_data={DELIVERY_EXCLUDED_KEY: OVER_QUOTA})
        assert is_actionable(row) is False

    def test_sql_twin_carries_the_exclusion_clause(self):
        sql = actionable_sql("r")
        assert DELIVERY_EXCLUDED_KEY in sql and OVER_QUOTA in sql
        assert "enrichment_data" in sql

    def test_address_only_rule_deliberately_ignores_the_marker(self):
        # The cap RANKS with this one; using the full rule would drop rows this
        # job already marked, so a re-run would renumber and mark a second batch.
        addr = address_actionable_sql("results")
        assert DELIVERY_EXCLUDED_KEY not in addr
        assert "property_address" in addr and "mailing_address" in addr
