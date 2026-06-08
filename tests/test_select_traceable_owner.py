"""Tests for select_traceable_owner — Phase 2 skip-trace owner selection.

Conservative, confidence-gated (Codex review): normal trace only on a confident
person owner; entities / estates / ambiguous 3-full-token names -> (None, None)
so the caller uses advanced (address-only) trace. Uses real captured King
multi-owner party_name examples.
"""
import pytest

from src.scrapers.enrichment.skip_trace import select_traceable_owner


@pytest.mark.parametrize("party,expected", [
    # Clean single person — WA "LAST FIRST [M]"
    ("WALKER WILLIAM H III", ("WILLIAM", "WALKER")),   # III is a suffix -> confident
    ("BAUS DONALD L", ("DONALD", "BAUS")),             # 3-token, middle initial
    ("LIVINGSTONE DAVID", ("DAVID", "LIVINGSTONE")),   # 2-token
    ("SMITH, JOHN A", ("JOHN", "SMITH")),              # comma format
    # Multi-owner: person beside an entity trustee/bank -> pick the person
    ("BOYLE DAVID E / QUALITY LOAN SERVICE CORP", ("DAVID", "BOYLE")),
    ("SEIFU ENDALKACHEW M / FIRST NATIONAL BANK OF AMERICA", ("ENDALKACHEW", "SEIFU")),
    # Two persons, one cleanly 2-token parseable -> pick the confident one
    ("ROBAR SERENA LYNN / ROBAR JASON", ("JASON", "ROBAR")),
    # Entity first, ambiguous 3-full-token person second -> no confident person
    ("KES REALESTATE PROPERTIES LLC / JONES PRESTON JANET", (None, None)),
    # Pure entities -> advanced
    ("QUALITY LOAN SERVICE CORP", (None, None)),
    ("M POWER CONSTRUCTION N DESIGN LLC", (None, None)),
    ("MARIE AND GIBSON HOLDINGS LLC", (None, None)),
    # Estate / trust / heirs -> advanced (deceased / proxy, not a living owner)
    ("INGRAM ROY W EST OF", (None, None)),
    ("SMITH FAMILY TRUST", (None, None)),
    # Ambiguous single 3-full-token name (no initial/suffix) -> advanced
    ("JONES PRESTON JANET", (None, None)),
    # Empty
    ("", (None, None)),
    (None, (None, None)),
])
def test_select_traceable_owner(party, expected):
    assert select_traceable_owner(party) == expected


def test_entity_first_person_second_when_person_is_clean():
    # Entity first but the person segment is a clean 2-token name -> pick person.
    assert select_traceable_owner("ACME HOLDINGS LLC / DOE JANE") == ("JANE", "DOE")


def test_does_not_pick_entity_even_if_only_owner():
    assert select_traceable_owner("PRIME RECON LLC") == (None, None)
