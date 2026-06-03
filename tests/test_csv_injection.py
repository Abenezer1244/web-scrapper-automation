"""Formula/CSV injection regression tests for sanitize_for_csv.

Covers the bypasses found in the 2026-06-01 red-team (E1 leading-quote, E3
embedded-tab) so they cannot silently regress. sanitize_for_csv is a pure
function — no DB/Redis needed.
"""
from src.api.middleware.security import sanitize_for_csv

_APO = "'"
_Q = '"'
_BT = "`"
_TAB = "\t"


def test_plain_text_is_untouched():
    assert sanitize_for_csv("John Smith") == "John Smith"
    assert sanitize_for_csv("123 Main St") == "123 Main St"


def test_leading_formula_chars_are_neutralized():
    for payload in ("=cmd|calc", "+1", "-2+3", "@SUM(A1)"):
        assert sanitize_for_csv(payload).startswith(_APO), payload


def test_leading_whitespace_then_formula_is_neutralized():
    assert sanitize_for_csv("   =1+1").startswith(_APO)


def test_leading_quote_bypass_is_neutralized():
    # E1: a spreadsheet strips wrapping quotes before evaluating, so a value
    # that begins with a quote then a formula must still be neutralized.
    for payload in (
        _Q + "=HYPERLINK(\"http://evil\")",
        _Q + _Q + "=1+1",
        _APO + "=1+1",
        _BT + "=1+1",
        "  " + _Q + "=1+1",
    ):
        assert sanitize_for_csv(payload).startswith(_APO), repr(payload)


def test_embedded_tab_is_stripped():
    # E3: an embedded TAB acts as a delimiter on TSV import / paste, splitting
    # the value into a new cell that begins with "=". The tab must be removed.
    out = sanitize_for_csv("123 Main St" + _TAB + "=cmd|calc")
    assert _TAB not in out


def test_none_and_empty():
    assert sanitize_for_csv(None) == ""
    assert sanitize_for_csv("") == ""
