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


# ── Vacant / raw land: no STREET, but a real structured situs ────────────────
# King's GIS matches the parcel and returns city+state+ZIP with no ADDR_FULL for
# roughly a third of its delinquent parcels (#153). property_address stays NULL on
# purpose — skip trace bills off it — but compose_situs early-returns on a falsy
# address, so the structured parts were being DISCARDED and the later owner-flag
# recompute wrote property_state back to None. out_of_state_owner needs only the two
# states, so a state we were actually handed must survive (Codex High, 2026-09-03).

_OWNER_OUT_OF_STATE = "PO BOX 1, PORTLAND, OR 97201"
_OWNER_IN_STATE = "123 X ST, SEATTLE, WA 98118"


def test_vacant_parcel_still_answers_out_of_state():
    f = compute_owner_flags(None, _OWNER_OUT_OF_STATE,
                            property_city="SEATTLE", property_state="WA", property_zip="98118")
    assert f["property_state"] == "WA"
    assert f["out_of_state_owner"] is True


def test_vacant_parcel_in_state_owner_is_a_real_false():
    f = compute_owner_flags(None, _OWNER_IN_STATE,
                            property_city="SEATTLE", property_state="WA", property_zip="98118")
    assert f["out_of_state_owner"] is False


def test_vacant_parcel_absentee_stays_unknown():
    # No street on the property side, so street comparison is impossible. None is
    # the honest answer — never a confident True.
    for owner in (_OWNER_OUT_OF_STATE, _OWNER_IN_STATE):
        f = compute_owner_flags(None, owner, property_city="SEATTLE",
                                property_state="WA", property_zip="98118")
        assert f["absentee_owner"] is None


def test_without_parts_a_streetless_row_is_unchanged():
    # The opt-in guarantee: no parts means byte-identical prior behaviour.
    f = compute_owner_flags(None, _OWNER_OUT_OF_STATE)
    assert f == {"property_state": None, "owner_state": "OR",
                 "absentee_owner": None, "out_of_state_owner": None}


def test_only_a_validated_two_letter_state_is_honoured():
    f = compute_owner_flags(None, _OWNER_OUT_OF_STATE, property_state="WASHINGTON")
    assert f["property_state"] is None
    assert f["out_of_state_owner"] is None
