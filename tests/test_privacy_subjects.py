"""Structured subjects from name cells (src/privacy/subjects.py; Phase 6, P1b).

Pure. Cells are the shapes BridgeLeads' scrapers store, read in the order each
source declares (src.utils.lead_export.name_order_for). The negative cases come
from the export's production fixtures (tests/test_lead_formatting.py) and the
Codex review of the first version, which guessed both word orders.
"""

import pytest

from src.privacy.subjects import (
    ENTITY_OR_PERSON,
    GIVEN_NAME_MISSING,
    KIND_ENTITY,
    KIND_PERSON,
    KIND_UNRESOLVED,
    NAME_ORDER_UNKNOWN,
    NON_LATIN_SCRIPT,
    NORMALIZATION_VERSION,
    ROLE_OR_CAPACITY,
    SURNAME_SHARED,
    UNREADABLE_IN_ORDER,
    email_key_value,
    normalize_text,
    person_key_value,
    phone_key_value,
    reused_rules_fingerprint,
    subject_key_values,
    subjects_from,
)
from src.utils.lead_export import name_order_for
from src.utils.lead_formatting import (
    NAME_ORDER_COMMA_ONLY,
    NAME_ORDER_NATURAL,
    NAME_ORDER_RECORDER,
)

REC, NAT, COMMA = NAME_ORDER_RECORDER, NAME_ORDER_NATURAL, NAME_ORDER_COMMA_ONLY
PINNED_FINGERPRINT = "fb399cce7198ba21fed82885e338268bf7de73ab7269917720ca42427a186f1f"


def _subs(raw, order, role="party"):
    return subjects_from(raw, role=role, source_field="party_name", name_order=order)


def _keys(raw, order):
    return [k for s in _subs(raw, order) for k in subject_key_values(s)]


# ── People: exactly one reading, in the declared order ──────────────────────

@pytest.mark.parametrize(("raw", "order"), [
    ("SMITH JOHN A", REC),
    ("SMITH JOHN A JR", REC),
    ("Smith, John A.", REC),
    ("Smith, John A.", COMMA),
    ("John A. Smith", NAT),
    ("John A. Smith Jr.", NAT),
])
def test_one_person_one_key_in_every_format(raw, order):
    assert _keys(raw, order) == [("person", "smith|john")]


def test_the_declared_order_is_trusted_never_both_readings():
    # The first version emitted both (smith, john) and (john, smith), so SMITH JOHN
    # and JOHN SMITH collided. Now each source yields exactly one reading.
    assert _keys("JOHN SMITH", REC) == [("person", "john|smith")]
    assert _keys("JOHN SMITH", NAT) == [("person", "smith|john")]
    assert _keys("SMITH JOHN", NAT) == [("person", "john|smith")]


def test_unknown_order_reads_no_person_but_still_types_entities():
    (sub,) = _subs("SMITH JOHN", None)
    assert sub.kind == KIND_UNRESOLVED and sub.review_reason == NAME_ORDER_UNKNOWN
    assert subject_key_values(sub) == []
    (ent,) = _subs("ACME HOLDINGS LLC", None)
    assert ent.kind == KIND_ENTITY
    # Pierce code violations store a case label, not a name; its order is None, so
    # no person can come out of it (the digits type it as an entity, which no
    # rule's entity name can equal).
    assert name_order_for("code_violation", "pierce") is None
    assert all(kind != "person" for kind, _v in _keys("Nuisance - 3711 S D ST", None))


def test_a_comma_only_source_reads_only_the_comma_form():
    assert _keys("JONES, GLENNA K", COMMA) == [("person", "jones|glenna")]
    assert _keys("GLENNA K JONES", COMMA) == []


def test_natural_order_comma_is_a_list_separator_not_last_first():
    assert _keys("Lee, Sang Ki and Lee, Hye Kyung", NAT) == []


# ── Parties and co-owners ────────────────────────────────────────────────────

def test_parties_split_and_never_blend():
    subs = _subs("SMITH JOHN A / DOE JANE B", REC)
    assert [(s.surname, s.given) for s in subs] == [("smith", "john"), ("doe", "jane")]


def test_a_shared_surname_is_a_review_suggestion_never_a_key():
    # The reading is suggested in the declared direction, but it is inferred, so it
    # routes to review rather than matching automatically (Codex).
    rec = _subs("SMITH JOHN A & MARY B", REC)
    assert [(s.kind, s.surname, s.given, s.review_reason) for s in rec] == [
        (KIND_PERSON, "smith", "john", None),
        (KIND_UNRESOLVED, "smith", "mary", SURNAME_SHARED)]
    nat = _subs("JOHN AND JANE SMITH, HUSBAND AND WIFE", NAT)
    assert [(s.kind, s.surname, s.given, s.review_reason) for s in nat] == [
        (KIND_UNRESOLVED, "smith", "john", SURNAME_SHARED),
        (KIND_PERSON, "smith", "jane", None)]
    assert _keys("SMITH JOHN A & MARY B", REC) == [("person", "smith|john")]


@pytest.mark.parametrize("raw", ["LUKINS & ANNIS", "PETRAKOPOULOS/ALEX AND SHANNON"])
def test_an_unreadable_party_stays_one_subject(raw):
    # No part reads as a person: one review item, not fragments of a firm's name.
    (sub,) = _subs(raw, REC)
    assert sub.review_reason == UNREADABLE_IN_ORDER and sub.text == normalize_text(raw)
    assert subject_key_values(sub) == []


@pytest.mark.parametrize("raw", ["AT&T", "B&B"])
def test_brand_ampersands_are_not_co_owners(raw):
    assert len(_subs(raw, REC)) == 1
    assert _keys(raw, REC) == []


def test_heirs_lists_keep_their_role():
    subs = _subs("DOE JANE; DOE ROBERT / ROE ALEX", REC, role="heir")
    assert [s.role for s in subs] == ["heir"] * 3
    assert subject_key_values(subs[2]) == [("person", "roe|alex")]


# ── Entities ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("raw", "etype"), [
    ("ACME HOLDINGS LLC", "company"),
    ("WELLS FARGO BANK NA", "company"),
    ("SAN MARCO L.L.P.", "company"),
    ("MINADOKA L L C", "company"),
    ("FOUR M ALLIANCE CORPORATION", "company"),
    ("SMITH FAMILY TRUST", "trust"),
    ("JOHN SMITH REVOCABLE TRUST", "trust"),
    ("KING COUNTY", "government"),
    ("DEPT OF NATURAL RESOURCES", "government"),
    ("WSDOT R/E SERVICES", "government"),
    ("ISLAMIC CENTER OF KENT", "other"),
])
def test_entities_are_typed_whole_and_keyed_by_name(raw, etype):
    (sub,) = _subs(raw, REC)
    assert sub.kind == KIND_ENTITY and sub.entity_type == etype
    assert sub.surname is None and sub.given is None
    assert subject_key_values(sub) == [("entity", normalize_text(raw))]


@pytest.mark.parametrize("raw", ["CHURCH JOHN", "BANK, JOHN", "NA MIN JUN", "STATE MARY",
                                 "CO JOHN SMITH JR", "CHURCH JOHN A"])
def test_a_surname_that_is_also_an_entity_word_is_unresolved_not_an_entity(raw):
    # Typing a person as an entity would silently miss that person's rule.
    (sub,) = _subs(raw, REC)
    assert sub.kind == KIND_UNRESOLVED and sub.review_reason == ENTITY_OR_PERSON
    assert subject_key_values(sub) == []


def test_a_marker_word_beside_an_org_function_word_stays_an_entity():
    (sub,) = _subs("CHURCH OF JESUS CHRIST", REC)
    assert sub.kind == KIND_ENTITY


def test_an_entity_party_beside_a_person_does_not_hide_the_person():
    subs = _subs("SMITH JOHN / WELLS FARGO BANK NA", REC)
    assert [s.kind for s in subs] == [KIND_PERSON, KIND_ENTITY]


@pytest.mark.parametrize(("raw", "order"), [
    ("SMITH JOHN & WELLS FARGO BANK NA", REC),
    ("WELLS FARGO BANK NA & SMITH JOHN", REC),
    ("JOHN SMITH AND ACME HOLDINGS LLC", NAT),
])
def test_a_co_owner_cell_mixing_a_person_and_an_entity_is_split(raw, order):
    # Codex: classifying the whole cell as an entity hid the person entirely.
    subs = _subs(raw, order)
    assert sorted(s.kind for s in subs) == [KIND_ENTITY, KIND_PERSON]
    assert ("person", "smith|john") in _keys(raw, order)


def test_a_mixed_cell_part_that_is_not_a_full_name_goes_to_review():
    subs = _subs("YESLER TOWERS LLC/CHAN J", REC)
    assert [(s.kind, s.review_reason) for s in subs] == [
        (KIND_ENTITY, None), (KIND_UNRESOLVED, GIVEN_NAME_MISSING)]


def test_a_weak_part_beside_a_strong_entity_is_not_swallowed():
    subs = _subs("CHURCH JOHN & ACME LLC", REC)
    assert [(s.kind, s.review_reason) for s in subs] == [
        (KIND_UNRESOLVED, ENTITY_OR_PERSON), (KIND_ENTITY, None)]


def test_a_cell_with_no_strong_part_goes_to_review_whole():
    (sub,) = _subs("CHURCH JOHN & BANK JANE", REC)
    assert sub.review_reason == ENTITY_OR_PERSON and subject_key_values(sub) == []


@pytest.mark.parametrize("raw", ["SMITH JOHN AS AGENT & ACME LLC",
                                 "LANGE CARL R MRS & ACME LLC"])
def test_a_mixed_cell_person_part_keeps_the_role_guards(raw):
    subs = _subs(raw, REC)
    assert [(s.kind, s.review_reason) for s in subs] == [
        (KIND_UNRESOLVED, ROLE_OR_CAPACITY), (KIND_ENTITY, None)]
    assert all(k == "entity" for k, _v in _keys(raw, REC))


@pytest.mark.parametrize("raw", ["ESTATE OF", ", A MARRIED MAN", " ; ", "---",
                                 "SMITH JOHN / ESTATE OF"])
def test_non_empty_text_is_never_silently_dropped(raw):
    subs = _subs(raw, REC)
    assert subs, "only empty input may yield no subject"
    assert subs[-1].kind == KIND_UNRESOLVED and subs[-1].review_reason == UNREADABLE_IN_ORDER


# ── Estates, roles, noise ────────────────────────────────────────────────────

def test_estates_are_marked_not_decided():
    (sub,) = _subs("ESTATE OF JOHN SMITH", NAT)
    assert (sub.kind, sub.capacity, sub.surname, sub.given) == (
        KIND_PERSON, "estate", "smith", "john")
    (sub,) = _subs("Est of Jane Doe", NAT)
    assert (sub.capacity, sub.surname) == ("estate", "doe")
    (sub,) = _subs("JOHNSON WILLIAM EST OF", REC)
    assert (sub.capacity, sub.surname, sub.given) == ("estate", "johnson", "william")
    # A recorder source writes both orders after the prefix (prod): comma form only.
    assert _keys("ESTATE OF RICHARD TODD", REC) == []
    assert _keys("ESTATE OF SMITH, JOHN", REC) == [("person", "smith|john")]


def test_a_fiduciary_role_tail_keeps_the_person_and_marks_the_capacity():
    (sub,) = _subs("ALDRIDGE FAYE MARIE TTEE", REC)
    assert (sub.kind, sub.capacity, sub.surname, sub.given) == (
        KIND_PERSON, "fiduciary", "aldridge", "faye")
    (sub,) = _subs("JOHN SMITH TRUSTEE", NAT)
    assert (sub.capacity, sub.surname) == ("fiduciary", "smith")


def test_et_al_is_noise_but_role_words_are_review_only():
    assert _keys("SMITH JOHN ET AL", REC) == [("person", "smith|john")]
    assert _keys("SMITH JOHN ETUX", REC) == [("person", "smith|john")]
    for raw in ("UNKNOWN HEIRS OF SMITH JOHN", "LANGE CARL R MRS", "SMITH JOHN AS AGENT"):
        (sub,) = _subs(raw, REC)
        assert sub.review_reason == ROLE_OR_CAPACITY and subject_key_values(sub) == []


@pytest.mark.parametrize("raw", ["LE MAI H", "LAVENDER A LORENE", "STEPHEN P MYERS",
                                 "VU NGUYEN SONG KHANH"])
def test_shapes_the_export_cannot_read_are_never_keyed(raw):
    assert _keys(raw, REC) == []


def test_a_surname_with_only_an_initial_is_unresolved():
    (sub,) = _subs("CHAN J", REC)
    assert sub.review_reason == GIVEN_NAME_MISSING and subject_key_values(sub) == []


def test_care_of_and_alias_tails_do_not_become_people():
    assert _keys("JOHN SMITH C/O JANE DOE", NAT) == [("person", "smith|john")]
    assert _keys("MENDOZA JOSE M AKA JOE MENDOZA", REC) == [("person", "mendoza|jose")]


# ── Scripts, particles, normalisation ───────────────────────────────────────

def test_diacritics_fold_and_compound_surnames_hold():
    assert _keys("NÚÑEZ GARCÍA JOSÉ", REC) == [("person", "nunez garcia|jose")]
    assert person_key_value("Núñez García", "José María") == "nunez garcia|jose"


def test_particles_stay_with_the_surname():
    assert _keys("DE LA CRUZ MARIA", REC) == [("person", "de la cruz|maria")]
    assert _keys("Maria de la Cruz", NAT) == [("person", "de la cruz|maria")]


@pytest.mark.parametrize("raw", ["王小明", "ИВАНОВ ИВАН", "SMITH 太郎"])
def test_non_latin_scripts_go_to_review(raw):
    (sub,) = _subs(raw, REC)
    assert sub.review_reason == NON_LATIN_SCRIPT and subject_key_values(sub) == []


def test_key_value_helpers_refuse_what_cannot_match():
    assert person_key_value("Smith", "") is None and person_key_value("", "John") is None
    assert phone_key_value("+1 (206) 555-1234 x9") == "2065551234"
    assert phone_key_value("555-1234") is None
    assert email_key_value("  Jane.Doe@Example.COM ") == "jane.doe@example.com"
    assert email_key_value("not an email") is None


# ── Contract ────────────────────────────────────────────────────────────────

def test_inputs_are_validated():
    assert _subs(None, REC) == [] and _subs("  ", REC) == []
    with pytest.raises(ValueError):
        subjects_from("SMITH JOHN", role="customer", source_field="party_name", name_order=REC)
    with pytest.raises(ValueError):
        subjects_from("SMITH JOHN", role="party", source_field="party_name", name_order="lastfirst")
    with pytest.raises(ValueError):
        subjects_from("SMITH JOHN", role="party", source_field="", name_order=REC)


def test_subjects_carry_their_provenance():
    (sub,) = subjects_from("SMITH JOHN", role="contact", source_field="skip_trace_name",
                           name_order=REC)
    assert (sub.role, sub.source_field, sub.name_order) == ("contact", "skip_trace_name", REC)


def test_normalization_version_is_pinned_to_the_reused_rules():
    # Rules store the version; changing how a name reads without bumping it would
    # compare two normalisations as one. The fingerprint covers the lead_formatting
    # rules reused here: if this fails, a formatter change touched them. When the
    # change alters how any name or phone reads, bump NORMALIZATION_VERSION; either
    # way, re-pin the fingerprint.
    assert (NORMALIZATION_VERSION, reused_rules_fingerprint()) == ("n2", PINNED_FINGERPRINT)
