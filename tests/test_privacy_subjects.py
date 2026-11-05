"""Structured subjects from recorder name cells (src/privacy/subjects.py; Phase 6, P1b).

Pure. The formats are the ones BridgeLeads' scrapers actually store: LandmarkWeb
"LAST FIRST M" with " / " between owners (base_scraper.normalize_party_text),
"Last, First" cells, assessor "First M Last", grantee lists in `heirs`, entities.
"""

import pytest

from src.privacy.subjects import (
    NORMALIZATION_VERSION,
    normalize_text,
    subject_key_values,
    subjects_from,
)


def _pairs(raw, role="party"):
    return [s.name_pairs for s in subjects_from(raw, role)]


def test_the_same_person_matches_across_formats():
    readings = [_pairs(raw)[0] for raw in (
        "SMITH JOHN A", "Smith, John A.", "John A. Smith", "John Smith Jr.", "SMITH JOHN A JR")]
    common = set.intersection(*(set(r) for r in readings))
    assert ("smith", "john") in common


def test_without_a_comma_both_orders_are_kept_but_only_as_exact_pairs():
    pairs = _pairs("SMITH JOHN")[0]
    assert pairs == {("smith", "john"), ("john", "smith")}
    # A pair, never a token bag: "john smith" does not match ("smith", "mary").
    assert ("smith", "mary") not in pairs


def test_a_comma_fixes_the_order():
    assert _pairs("Smith, John")[0] == {("smith", "john")}


def test_multi_owner_cells_split_into_separate_subjects():
    subs = subjects_from("SMITH JOHN A / DOE JANE B", "party")
    assert len(subs) == 2
    assert ("smith", "john") in subs[0].name_pairs
    assert ("doe", "jane") in subs[1].name_pairs
    assert ("smith", "jane") not in subs[1].name_pairs  # owners never blend


def test_a_shared_surname_is_carried_to_a_lone_given_name():
    subs = subjects_from("SMITH JOHN A & MARY B", "party")
    assert len(subs) == 2
    assert ("smith", "mary") in subs[1].name_pairs


def test_and_and_aka_split_too():
    assert len(subjects_from("JOHN SMITH AND MARY SMITH", "party")) == 2
    aka = subjects_from("JOHN SMITH AKA JOHNNY SMITH", "party")
    assert len(aka) == 2 and ("smith", "johnny") in aka[1].name_pairs


def test_heirs_lists_are_split_one_subject_per_heir():
    subs = subjects_from("DOE JANE; DOE ROBERT / ROE ALEX", "heir")
    assert [s.role for s in subs] == ["heir"] * 3
    assert ("roe", "alex") in subs[2].name_pairs


@pytest.mark.parametrize(("raw", "etype"), [
    ("ACME HOLDINGS LLC", "company"),
    ("SMITH FAMILY TRUST", "trust"),
    ("JOHN SMITH TRUSTEE", "trust"),
    ("ESTATE OF JOHN SMITH", "estate"),
    ("Est of Jane Doe", "estate"),
    ("KING COUNTY", "government"),
    ("WELLS FARGO BANK NA", "company"),
])
def test_entities_are_classified_and_carry_no_person_pairs(raw, etype):
    (sub,) = subjects_from(raw, "owner_of_record")
    assert sub.kind == "entity" and sub.entity_type == etype
    assert sub.name_pairs == frozenset()
    assert subject_key_values(sub) == [("entity", sub.normalized)]


def test_estate_needs_of_so_a_surname_or_street_estate_is_not_an_entity():
    (sub,) = subjects_from("ESTATE JOHN", "party")
    assert sub.kind == "person"


def test_particles_stay_with_the_surname_in_both_orders():
    assert ("de la cruz", "maria") in _pairs("DE LA CRUZ MARIA")[0]
    assert ("de la cruz", "maria") in _pairs("Maria de la Cruz")[0]


def test_diacritics_and_punctuation_do_not_split_a_person():
    assert normalize_text("José  Núñez-García") == normalize_text("JOSE NUNEZ GARCIA")


def test_noise_and_initials_never_make_a_matchable_person():
    # A lone token or initials cannot identify anyone: review only, no key values.
    for raw in ("SMITH", "J SMITH", "J A", "ET AL"):
        for sub in subjects_from(raw, "party"):
            assert sub.name_pairs == frozenset()
            assert subject_key_values(sub) == []
    assert subjects_from("SMITH JOHN ET AL", "party")[0].name_pairs >= {("smith", "john")}


def test_empty_input_and_unknown_role():
    assert subjects_from(None, "party") == [] and subjects_from("  ", "party") == []
    with pytest.raises(ValueError):
        subjects_from("SMITH JOHN", "customer")


def test_key_values_are_surname_given_strings():
    (sub,) = subjects_from("Smith, John", "party")
    assert subject_key_values(sub) == [("person", "smith|john")]


def test_normalization_version_is_pinned():
    # Rules store this; changing the rules above without bumping it would compare
    # values from two normalisations as if they were one.
    assert NORMALIZATION_VERSION == "n1"
