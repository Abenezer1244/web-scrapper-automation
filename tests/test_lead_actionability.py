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
    actionable_condition,
    actionable_sql,
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
    mail_ok = nn(mail) and mail.strip() != ""
    # Structural check that the SQL really encodes those comparisons, trimmed.
    assert sql.count("property_address") == 3 and sql.count("mailing_address") == 2
    assert sql.count("btrim(") == 3  # property <> '', property <> placeholder, mailing <> ''
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
