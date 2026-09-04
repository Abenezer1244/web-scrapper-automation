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


class TestSuffixlessSitus:
    """Pierce County GIS Site_Address drops the suffix / post-directional that the
    same parcel's Delivery_Address keeps. That is the same base street — NOT proof
    the owner lives elsewhere. Pinned from prod rows (Test 1, 2026-09-02)."""

    def test_missing_post_directional_is_unknown_not_absentee(self):
        # parcel 8996011270: Site_Address vs Delivery_Address, both Lake Tapps
        f = compute_owner_flags(
            "20508 ISLAND PKWY", "20508 ISLAND PKWY E, LAKE TAPPS, WA, 98391-9081"
        )
        assert f["absentee_owner"] is None

    def test_missing_suffix_is_unknown_not_absentee(self):
        # parcel 5275000700: "1006 S 34TH" vs "1006 S 34TH ST"
        f = compute_owner_flags("1006 S 34TH", "1006 S 34TH ST, TACOMA, WA, 98418-4003")
        assert f["absentee_owner"] is None

    def test_missing_suffix_with_matching_zip_is_same(self):
        f = compute_owner_flags(
            "1006 S 34TH, TACOMA, WA 98418", "1006 S 34TH ST, TACOMA, WA 98418"
        )
        assert f["absentee_owner"] is False

    def test_missing_suffix_with_different_zip_is_absentee(self):
        f = compute_owner_flags(
            "1006 S 34TH, TACOMA, WA 98418", "1006 S 34TH ST, SEATTLE, WA 98101"
        )
        assert f["absentee_owner"] is True

    def test_dropped_leading_directional_is_still_different(self):
        # Only TRAILING tokens are tolerated — "E MAIN" vs "MAIN" are different streets.
        f = compute_owner_flags("123 E MAIN ST", "123 MAIN ST, TACOMA, WA 98401")
        assert f["absentee_owner"] is True

    def test_extra_non_suffix_token_is_still_different(self):
        f = compute_owner_flags("100 S", "100 S MAIN ST, TACOMA, WA 98401")
        assert f["absentee_owner"] is True

    def test_different_house_number_is_still_different(self):
        f = compute_owner_flags("1008 S 34TH", "1006 S 34TH ST, TACOMA, WA, 98418")
        assert f["absentee_owner"] is True

    def test_genuinely_different_street_is_absentee(self):
        # parcel 7002180980: property in Bonney Lake, owner mails to Lake Tapps
        f = compute_owner_flags(
            "2715 67TH CT SE", "20508 ISLAND PKWY E, LAKE TAPPS, WA, 98391-9081"
        )
        assert f["absentee_owner"] is True


class TestCommalessSitusCity:
    """An NTS notice prints the situs with NO comma before the city ("Commonly known
    as: 1207 118TH PL SW EVERETT, WASHINGTON 98204-4813"), so the comma-splitting
    display parser reads the city as part of the STREET while the owner's mailing
    address parses it out. Same place, two different street strings.

    Pinned from the prod "Test 5" rows (Snohomish trustee-sale leads, 2026-09-02).
    """

    def test_glued_city_with_zip4_vs_zip5_is_not_absentee(self):
        """The bug: identical addresses, but the situs carries ZIP+4 and the mailing
        ZIP5, so the whole-string shortcut missed and the glued city read as a
        different street — a confident absentee=True for an owner living in the house.
        """
        f = compute_owner_flags(
            "1207 118TH PL SW EVERETT, WA 98204-4813",
            "1207 118TH PL SW, EVERETT, WA 98204",
        )
        assert f["absentee_owner"] is False

    def test_glued_city_with_identical_zips_still_not_absentee(self):
        """The sibling row that happened to work before (both sides ZIP+4) — it must
        keep working, and now for the right reason rather than by string luck."""
        f = compute_owner_flags(
            "712 143RD PL SW LYNNWOOD, WA 98087-6429",
            "712 143RD PL SW, LYNNWOOD, WA 98087-6429",
        )
        assert f["absentee_owner"] is False

    def test_glued_city_with_a_different_zip_is_still_absentee(self):
        """The tolerance only DEFERS to the ZIP/city discriminator — it never asserts
        same-place on its own. A real absentee owner must still be flagged."""
        f = compute_owner_flags(
            "1207 118TH PL SW EVERETT, WA 98204-4813",
            "1207 118TH PL SW, SEATTLE, WA 98101",
        )
        assert f["absentee_owner"] is True

    def test_trailing_token_must_match_the_other_sides_own_city(self):
        """Matched on the OTHER side's parsed city, not on any city-looking token:
        a genuine extra street word is still a different street."""
        f = compute_owner_flags(
            "1207 118TH PL SW BOTHELL, WA 98204",
            "1207 118TH PL SW, EVERETT, WA 98204",
        )
        assert f["absentee_owner"] is True

    def test_glued_city_without_a_discriminator_stays_unknown(self):
        """No ZIP on the situs side: same street modulo the city is not proof of
        owner-occupancy, so the honest answer is unknown, not False."""
        f = compute_owner_flags(
            "1207 118TH PL SW EVERETT",
            "1207 118TH PL SW, EVERETT, WA 98204",
        )
        assert f["absentee_owner"] is None

    def test_a_property_that_parsed_its_own_city_is_left_alone(self):
        """The tolerance is keyed to the comma-less situs shape, where the parser
        finds NO city because the city is glued to the street. When the property
        address DID parse a city of its own, a trailing word is part of the street
        name and must not be dropped — behaviour here is unchanged from before the
        fix (Codex review)."""
        f = compute_owner_flags(
            "1207 118TH PL SW EVERETT, EVERETT, WA 98204",
            "1207 118TH PL SW, EVERETT, WA 98204",
        )
        assert f["absentee_owner"] is True

    def test_a_bare_house_number_is_not_a_street_to_absorb_into(self):
        """"123, EVERETT, WA 98204" parses to street "123" + city "EVERETT". Without
        a letter in it that is not a street, and absorbing it would let any
        "123 <CITY>" in the same ZIP match it (Codex review). These two normalize to
        the same full string, so the answer comes from the pre-existing whole-string
        shortcut — this test pins that the new helper does not widen it."""
        f = compute_owner_flags("123 EVERETT, WA 98204", "456 MAPLE ST, EVERETT, WA 98204")
        assert f["absentee_owner"] is True
