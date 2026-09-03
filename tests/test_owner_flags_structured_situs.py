"""compute_owner_flags with the structured situs parts (migration 085).

Real Test 3 rows (2026-09-02): the county says Barbara Hill's mail goes to the
property ("22109 43RD AVE E, SPANAWAY, WA, 98387-6887"), but with a street-only
property_address the flag stayed unknown. With the situs city/zip from the notice
("22109 43RD AVENUE EAST, SPANAWAY, WA 98387") it becomes a real False.
"""
from src.utils.address_intel import compose_situs, compute_owner_flags

_STREET = "22109 43RD AVE E"
_MAIL_SAME = "22109 43RD AVE E, SPANAWAY, WA, 98387-6887"
_MAIL_AWAY = "8402 S AINSWORTH AVE, TACOMA, WA, 98444-4414"


def test_without_parts_behaviour_is_unchanged():
    f = compute_owner_flags(_STREET, _MAIL_SAME)
    assert f["absentee_owner"] is None and f["property_state"] is None
    assert f["out_of_state_owner"] is None


def test_with_parts_same_place_is_a_real_false():
    f = compute_owner_flags(_STREET, _MAIL_SAME, property_city="SPANAWAY",
                            property_state="WA", property_zip="98387")
    assert f["absentee_owner"] is False
    assert f["property_state"] == "WA" and f["owner_state"] == "WA"
    assert f["out_of_state_owner"] is False


def test_with_parts_different_place_is_true():
    f = compute_owner_flags(_STREET, _MAIL_AWAY, property_city="SPANAWAY",
                            property_state="WA", property_zip="98387")
    assert f["absentee_owner"] is True and f["out_of_state_owner"] is False


def test_out_of_state_now_computable():
    f = compute_owner_flags(_STREET, "11 WARREN ST #2, SALEM, MA, 01970-3119",
                            property_city="SPANAWAY", property_state="WA", property_zip="98387")
    assert f["out_of_state_owner"] is True and f["absentee_owner"] is True


def test_no_mailing_stays_unknown_even_with_parts():
    f = compute_owner_flags(_STREET, None, property_city="SPANAWAY", property_state="WA")
    assert f["absentee_owner"] is None and f["owner_state"] is None
    assert f["property_state"] == "WA"


def test_compose_situs_appends_only_what_exists():
    assert compose_situs(_STREET) == _STREET
    assert compose_situs(_STREET, "SPANAWAY") == "22109 43RD AVE E, SPANAWAY"
    assert compose_situs(_STREET, None, "WA", "98387") == "22109 43RD AVE E, WA 98387"
    assert compose_situs(_STREET, "SPANAWAY", "WA", "98387") == "22109 43RD AVE E, SPANAWAY, WA 98387"
    assert compose_situs(None, "SPANAWAY") is None
