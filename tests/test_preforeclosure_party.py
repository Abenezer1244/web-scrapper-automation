"""Regression tests for pre_foreclosure party orientation, built from REAL strings
captured live from the county portals (no network, no browser).

The lead is the distressed HOMEOWNER. These lock in that the borrower lands in
party_name and the trustee/lender/servicer/law-firm context moves to heirs — across
the comma-stacked, "(+)"-suffixed, and known-token-less-trustee shapes that the
production diagnostic surfaced.
"""
import pytest

from src.scrapers.pierce_wa_probate import _strip_arms_plus
from src.scrapers.preforeclosure import (
    is_person_name,
    orient_pre_foreclosure_party,
    strip_vesting_clause,
)
from src.scrapers.sources.nts_pdf import normalize_pdf_text

# Real Pierce ARMS [R]/[E] rows (captured live) → expected (party_name, heirs).
# None = the row must be DROPPED (bank-vs-trustee, no recoverable homeowner).
_PIERCE_ARMS = [
    ("TRUSTEE CORPS(+)", "MACON MONTREUX", ("MACON MONTREUX", "TRUSTEE CORPS")),
    ("SHELLPOINT MORTGAGE SERVICING(+)", "WILLIAMS RACHEL(+)",
     ("WILLIAMS RACHEL", "SHELLPOINT MORTGAGE SERVICING")),
    # [R] already the person — keep, no swap.
    ("MATTEO CAROLYN C", "ELMWOOD MOBILE HOME PARK",
     ("MATTEO CAROLYN C", "ELMWOOD MOBILE HOME PARK")),
    # Living-trust trustee on a person is still the owner.
    ("QUALITY LOAN SERVICE CORP(+)", "KALLANSRUD KEVIN KENT TR(+)",
     ("KALLANSRUD KEVIN KENT TR", "QUALITY LOAN SERVICE CORP")),
    # Token-less trustee brand ("WESTERN PROGRESSIVE") must NOT pass as the person.
    ("WESTERN PROGRESSIVE-WASHINGTON(+)", "ALLEYNE MARCUS(+)",
     ("ALLEYNE MARCUS", "WESTERN PROGRESSIVE-WASHINGTON")),
    ("WELLS FARGO BANK TR(+)", "FARLEY LEVI", ("FARLEY LEVI", "WELLS FARGO BANK TR")),
]


@pytest.mark.parametrize("raw_r,raw_e,expected", _PIERCE_ARMS)
def test_pierce_arms_orientation(raw_r, raw_e, expected):
    oriented = orient_pre_foreclosure_party(_strip_arms_plus(raw_r), _strip_arms_plus(raw_e))
    assert oriented == expected


def test_arms_plus_marker_stripped():
    assert _strip_arms_plus("QUALITY LOAN SERVICE CORP(+)") == "QUALITY LOAN SERVICE CORP"
    assert _strip_arms_plus("MACON MONTREUX(+)") == "MACON MONTREUX"
    assert _strip_arms_plus(None) is None
    assert _strip_arms_plus("") in (None, "")


def test_bank_vs_trustee_row_drops():
    """No person on either side → None (caller drops; not a homeowner lead)."""
    assert orient_pre_foreclosure_party("QUALITY LOAN SERVICE CORP", "NORTH STAR TRUSTEE LLC") is None


def test_western_progressive_is_not_a_person():
    assert is_person_name("WESTERN PROGRESSIVE-WASHINGTON") is False
    # but a real two-token person still passes
    assert is_person_name("ALLEYNE MARCUS") is True
    assert is_person_name("MACON MONTREUX") is True


# Snohomish Tribune NTS grantor lines (captured live) — vesting boilerplate stripped,
# co-borrower "AND" connector preserved.
_VESTING = [
    ("MICHAEL A. BRANDT, A MARRIED MAN, AS HIS SOLE AND SEPARATE PROPERTY",
     "MICHAEL A. BRANDT"),
    ("MICHAEL A OLDEN, AN UNMARRIED MAN, AS HIS SEPARATE ESTATE", "MICHAEL A OLDEN"),
    ("VADAD SOLEIMANZADEH, AN UNMARRIED PERSON, AND YAHYA KAZEMIKARANI, AN UNMARRIED PERSON",
     "VADAD SOLEIMANZADEH AND YAHYA KAZEMIKARANI"),
    ("JANE DOE AND JOHN DOE, HUSBAND AND WIFE", "JANE DOE AND JOHN DOE"),
    # no vesting → unchanged
    ("ACME HOLDINGS LLC", "ACME HOLDINGS LLC"),
]


@pytest.mark.parametrize("raw,expected", _VESTING)
def test_strip_vesting_clause(raw, expected):
    assert strip_vesting_clause(raw) == expected


def test_pdf_dehyphenation_space_before_hyphen():
    """Snohomish layout wraps as 'MI -\\nCHAEL'; the soft hyphen + wrap must be joined."""
    raw = "Grantor: MI -\nCHAEL A. BRANDT, AS HIS SOLE AND SEPARATE PROP -\nERTY"
    out = normalize_pdf_text(raw)
    assert "MICHAEL" in out
    assert "PROPERTY" in out
    assert "MI -" not in out and "PROP -" not in out


def test_pdf_dehyphenation_keeps_digit_identifier():
    """A wrapped TS#/parcel ('WA-25-\\n1012820') keeps its real hyphen."""
    out = normalize_pdf_text("Trustee Sale No.: WA-25-\n1012820")
    assert "WA-25-1012820" in out
