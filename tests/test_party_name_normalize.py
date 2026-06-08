"""Tests for normalize_party_text — multi-owner name de-concatenation.

Uses the EXACT raw grantor HTML captured from King LandmarkWeb's live
GetSearchResults response, where stacked parties are separated by
`<div class='nameSeperator'></div>` (sic). Blanket tag-stripping previously
dropped that separator with no replacement, concatenating distinct parties.
"""
import pytest

from src.scrapers.base_scraper import normalize_party_text

SEP = "<div class='nameSeperator'></div>"


@pytest.mark.parametrize("raw,expected", [
    # Real captured King examples (the bug)
    (f"BOYLE DAVID E{SEP}QUALITY LOAN SERVICE CORP", "BOYLE DAVID E / QUALITY LOAN SERVICE CORP"),
    (f"ROBAR SERENA LYNN{SEP}ROBAR JASON", "ROBAR SERENA LYNN / ROBAR JASON"),
    (f"STEPHEN P MYERS {SEP}ROBBINS GEORGIA A", "STEPHEN P MYERS / ROBBINS GEORGIA A"),
    (f"SEIFU ENDALKACHEW M{SEP}FIRST NATIONAL BANK OF AMERICA",
     "SEIFU ENDALKACHEW M / FIRST NATIONAL BANK OF AMERICA"),
    (f"BAILEY LACIA LYNNE{SEP}MOSKOWITZ BENJAMIN M", "BAILEY LACIA LYNNE / MOSKOWITZ BENJAMIN M"),
    # Single party — unchanged
    ("WALKER WILLIAM H III", "WALKER WILLIAM H III"),
    ("M POWER CONSTRUCTION N DESIGN LLC", "M POWER CONSTRUCTION N DESIGN LLC"),
    ("MARIE AND GIBSON HOLDINGS LLC", "MARIE AND GIBSON HOLDINGS LLC"),
    # <br> as a separator (other portals)
    ("SMITH JOHN A<br>SMITH JANE B", "SMITH JOHN A / SMITH JANE B"),
    ("SMITH JOHN A<br/>SMITH JANE B", "SMITH JOHN A / SMITH JANE B"),
    # Inline markup must NOT split a single name mid-token
    ("MA<b>RRS</b> DONALD E", "MARRS DONALD E"),
    # HTML entities decoded
    ("SMITH &amp; JONES LLC", "SMITH & JONES LLC"),
    # LandmarkWeb CSS-class prefix stripped
    ("nobreak_SMITH JOHN", "SMITH JOHN"),
    ("unclickable_DOE JANE", "DOE JANE"),
    # Empty / None
    ("", ""),
    (None, ""),
])
def test_normalize_party_text(raw, expected):
    assert normalize_party_text(raw) == expected


def test_three_owners_all_separated():
    raw = f"A AAA{SEP}B BBB{SEP}C CCC"
    assert normalize_party_text(raw) == "A AAA / B BBB / C CCC"


def test_no_leading_or_trailing_delimiter():
    raw = f"{SEP}SOLO NAME{SEP}"
    assert normalize_party_text(raw) == "SOLO NAME"


def test_idempotent_on_already_clean_value():
    val = "BOYLE DAVID E / QUALITY LOAN SERVICE CORP"
    assert normalize_party_text(val) == val
