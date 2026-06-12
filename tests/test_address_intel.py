"""Owner-location flag tests (src/utils/address_intel.py).

Real parsing, no mocks. Pins the absentee/out-of-state semantics Codex specified:
component compare, unit-only difference is NOT absentee, NULL when unknown.
"""
from src.utils.address_intel import _normalize_street, compute_owner_flags


class TestNormalizeStreet:
    def test_unit_stripped(self):
        assert _normalize_street("123 MAIN ST APT 2") == _normalize_street("123 MAIN ST")

    def test_hash_unit_stripped(self):
        assert _normalize_street("123 MAIN ST #5") == "123 MAIN ST"

    def test_suffix_canonicalized(self):
        assert _normalize_street("123 MAIN STREET") == _normalize_street("123 MAIN ST")

    def test_directional_canonicalized(self):
        assert _normalize_street("123 NORTH MAIN ST") == _normalize_street("123 N MAIN ST")

    def test_punctuation_and_case(self):
        assert _normalize_street("123 main st.") == "123 MAIN ST"

    def test_box_in_street_name_not_stripped(self):
        # 'BOX CANYON RD' is a real street, not a PO box (Codex review)
        assert _normalize_street("123 BOX CANYON RD") == "123 BOX CANYON RD"

    def test_no_in_street_name_not_stripped(self):
        assert _normalize_street("10 NO NAME RD") == "10 NO NAME RD"

    def test_no_with_unit_number_is_stripped(self):
        # 'NO 4' (a unit number) IS a secondary designator
        assert _normalize_street("123 MAIN ST NO 4") == "123 MAIN ST"

    def test_unit_letter_stripped(self):
        assert _normalize_street("123 MAIN ST UNIT B") == "123 MAIN ST"


class TestOutOfState:
    def test_different_states_true(self):
        f = compute_owner_flags("1 A ST, TACOMA, WA 98401", "9 B AVE, PORTLAND, OR 97201")
        assert f["out_of_state_owner"] is True
        assert f["property_state"] == "WA" and f["owner_state"] == "OR"

    def test_same_state_false(self):
        f = compute_owner_flags("1 A ST, TACOMA, WA 98401", "9 B AVE, SEATTLE, WA 98101")
        assert f["out_of_state_owner"] is False

    def test_unknown_state_is_none(self):
        f = compute_owner_flags("1 A ST", "9 B AVE, SEATTLE, WA 98101")
        assert f["out_of_state_owner"] is None  # property state unparseable -> unknown


class TestAbsentee:
    def test_clearly_different_address_true(self):
        f = compute_owner_flags(
            "123 MAIN ST, TACOMA, WA 98401", "999 ELSEWHERE AVE, MIAMI, FL 33101"
        )
        assert f["absentee_owner"] is True

    def test_same_address_false(self):
        f = compute_owner_flags(
            "123 MAIN ST, TACOMA, WA 98401", "123 MAIN ST, TACOMA, WA 98401"
        )
        assert f["absentee_owner"] is False

    def test_unit_only_difference_not_absentee(self):
        # situs omits the unit the mailing carries — same building, NOT absentee
        f = compute_owner_flags(
            "123 MAIN ST, TACOMA, WA 98401", "123 MAIN ST APT 4, TACOMA, WA 98401"
        )
        assert f["absentee_owner"] is False

    def test_suffix_spelling_difference_not_absentee(self):
        f = compute_owner_flags(
            "123 MAIN STREET, TACOMA, WA 98401", "123 MAIN ST, TACOMA, WA 98401"
        )
        assert f["absentee_owner"] is False

    def test_same_street_different_zip_is_absentee(self):
        # same street text but different ZIP = different place (different city block)
        f = compute_owner_flags(
            "123 MAIN ST, TACOMA, WA 98401", "123 MAIN ST, SEATTLE, WA 98101"
        )
        assert f["absentee_owner"] is True

    def test_same_street_no_discriminator_is_none(self):
        # same normalized street (suffix spelling differs) but no ZIP/city/state
        # to confirm same building -> unknown, NOT a guessed False
        f = compute_owner_flags("123 MAIN ST", "123 MAIN STREET")
        assert f["absentee_owner"] is None

    def test_identical_addresses_is_false(self):
        f = compute_owner_flags("123 MAIN ST", "123 MAIN ST")
        assert f["absentee_owner"] is False  # byte-identical = confirmed same

    def test_same_street_no_zip_different_city_is_absentee(self):
        f = compute_owner_flags("123 MAIN ST, TACOMA, WA", "123 MAIN ST, SEATTLE, WA")
        assert f["absentee_owner"] is True

    def test_exact_full_string_match_is_false(self):
        # no parsed street structure but identical strings -> confirmed same
        f = compute_owner_flags("PARCEL ONLY 123", "PARCEL ONLY 123")
        assert f["absentee_owner"] is False

    def test_missing_mailing_is_none(self):
        f = compute_owner_flags("123 MAIN ST, TACOMA, WA 98401", None)
        assert f["absentee_owner"] is None

    def test_missing_property_is_none(self):
        f = compute_owner_flags(None, "123 MAIN ST, TACOMA, WA 98401")
        assert f["absentee_owner"] is None

    def test_both_missing_all_none(self):
        f = compute_owner_flags(None, None)
        assert f == {
            "property_state": None, "owner_state": None,
            "absentee_owner": None, "out_of_state_owner": None,
        }
