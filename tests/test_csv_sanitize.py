"""CSV formula-injection sanitizer tests (security review HIGH-6).

The prior bug: clean_text() stripped/space-replaced leading TAB/CR/LF before
the formula-prefix check ran, so those prefixes were silently NOT neutralized.
These tests pin the raw-value-first behavior and non-str coercion.
"""
from datetime import date

import pytest

from src.api.middleware.security import sanitize_for_csv


@pytest.mark.parametrize("payload", ["=cmd|' /C calc'!A1", "+1+1", "-2+3", "@SUM(A1)"])
def test_neutralizes_formula_prefixes(payload):
    out = sanitize_for_csv(payload)
    assert out.startswith("'"), f"{payload!r} not neutralized -> {out!r}"


@pytest.mark.parametrize("lead", ["\t", "\r", "\n"])
def test_neutralizes_leading_tab_cr_lf(lead):
    # These are the cases the old code silently missed: clean_text would
    # strip/space them before the check. Must be quoted now.
    out = sanitize_for_csv(lead + "=HYPERLINK(\"http://evil\")")
    assert out.startswith("'"), f"leading {lead!r} not neutralized -> {out!r}"


@pytest.mark.parametrize("payload", [" \t=cmd", "  =HYPERLINK(\"x\")", " \r+1", "\t \n@SUM(A1)"])
def test_neutralizes_leading_whitespace_then_formula(payload):
    # The bypass Codex caught: leading whitespace makes raw.startswith() False,
    # but clean_text strips it to reveal a formula char. Must still be quoted.
    out = sanitize_for_csv(payload)
    assert out.startswith("'"), f"{payload!r} bypassed sanitizer -> {out!r}"


def test_plain_value_unchanged():
    assert sanitize_for_csv("123 Main St") == "123 Main St"
    assert sanitize_for_csv("Smith, John") == "Smith, John"


def test_none_becomes_empty():
    assert sanitize_for_csv(None) == ""


def test_non_str_is_coerced_not_crashed():
    # The old function passed non-str straight to a regex .sub and could raise.
    assert sanitize_for_csv(2026) == "2026"
    assert sanitize_for_csv(date(2026, 6, 1)) == "2026-06-01"


def test_control_chars_still_stripped():
    # clean_text hygiene still applies to the (quoted) value.
    out = sanitize_for_csv("=A1\x00\x07B2")
    assert out.startswith("'")
    assert "\x00" not in out and "\x07" not in out
