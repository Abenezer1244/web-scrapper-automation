"""Tests for dialer-CSV display formatting (src/utils/lead_formatting.py)."""
from src.utils.lead_formatting import (
    normalize_phone_for_dialer,
    parse_property_for_display,
    split_owner_for_display,
)


class TestNormalizePhoneForDialer:
    def test_already_bare_10_digit(self):
        assert normalize_phone_for_dialer("2065551234") == "2065551234"

    def test_strips_formatting(self):
        assert normalize_phone_for_dialer("(206) 555-1234") == "2065551234"
        assert normalize_phone_for_dialer("206-555-1234") == "2065551234"
        assert normalize_phone_for_dialer("206.555.1234") == "2065551234"

    def test_drops_leading_country_code(self):
        assert normalize_phone_for_dialer("12065551234") == "2065551234"
        assert normalize_phone_for_dialer("+1 (206) 555-1234") == "2065551234"

    def test_strips_extension(self):
        assert normalize_phone_for_dialer("206-555-1234 x123") == "2065551234"
        assert normalize_phone_for_dialer("2065551234 ext 5") == "2065551234"
        # Punctuated extension forms must not blank a valid base number.
        assert normalize_phone_for_dialer("2065551234 ext: 7") == "2065551234"
        assert normalize_phone_for_dialer("2065551234 x. 7") == "2065551234"
        assert normalize_phone_for_dialer("(206) 555-1234 extension: 12") == "2065551234"

    def test_invalid_returns_blank(self):
        assert normalize_phone_for_dialer("555-1234") == ""        # 7 digits
        assert normalize_phone_for_dialer("not a phone") == ""
        assert normalize_phone_for_dialer("449900112233") == ""    # 12 digits, non-US
        assert normalize_phone_for_dialer(None) == ""
        assert normalize_phone_for_dialer("") == ""


class TestSplitOwnerForDisplay:
    def test_recorder_last_first(self):
        assert split_owner_for_display("SMITH JOHN") == ("JOHN", "SMITH")

    def test_recorder_last_first_middle(self):
        # 'SMITH JOHN MICHAEL' -> first=JOHN, last=SMITH (middle ignored)
        assert split_owner_for_display("SMITH JOHN MICHAEL") == ("JOHN", "SMITH")

    def test_comma_last_first(self):
        assert split_owner_for_display("SMITH, JOHN M") == ("JOHN", "SMITH")

    def test_multi_owner_picks_person_beside_entity(self):
        # The bank/trustee is rejected; the real person is surfaced.
        assert split_owner_for_display(
            "BOYLE DAVID E / QUALITY LOAN SERVICE CORP"
        ) == ("DAVID", "BOYLE")

    def test_entity_llc_yields_blank(self):
        assert split_owner_for_display("ACME PROPERTIES LLC") == (None, None)

    def test_entity_trust_yields_blank(self):
        assert split_owner_for_display("JOHN SMITH FAMILY TRUST") == (None, None)

    def test_digits_treated_as_entity(self):
        assert split_owner_for_display("UNIT 405 HOLDINGS") == (None, None)

    def test_estate_prefix_natural_order(self):
        # 'ESTATE OF JOHN SMITH' is natural order -> first=JOHN, last=SMITH
        assert split_owner_for_display("ESTATE OF JOHN SMITH") == ("JOHN", "SMITH")

    def test_compound_surname_comma(self):
        # Full pre-comma chunk is the surname (Codex P2).
        assert split_owner_for_display("DE LA CRUZ, MARIA") == ("MARIA", "DE LA CRUZ")

    def test_compound_surname_recorder_particle(self):
        # Leading particle binds to the surname: 'VAN DYKE JOHN' -> JOHN / VAN DYKE.
        assert split_owner_for_display("VAN DYKE JOHN") == ("JOHN", "VAN DYKE")

    def test_compound_surname_recorder_double_particle(self):
        # A run of particles binds: 'DE LA CRUZ MARIA' -> MARIA / DE LA CRUZ.
        assert split_owner_for_display("DE LA CRUZ MARIA") == ("MARIA", "DE LA CRUZ")

    def test_plain_two_token_recorder_unaffected(self):
        # 'VAN JOHN' (2 tokens) stays simple LAST FIRST -> JOHN / VAN.
        assert split_owner_for_display("VAN JOHN") == ("JOHN", "VAN")

    def test_single_token_is_surname(self):
        assert split_owner_for_display("MADONNA") == (None, "MADONNA")

    def test_empty_and_none(self):
        assert split_owner_for_display(None) == (None, None)
        assert split_owner_for_display("   ") == (None, None)


class TestParsePropertyForDisplay:
    def test_full_comma_three_part(self):
        out = parse_property_for_display("123 MAIN ST, TACOMA, WA 98401")
        assert out == {"street": "123 MAIN ST", "city": "TACOMA", "state": "WA", "zip": "98401"}

    def test_four_part_split_state_zip(self):
        out = parse_property_for_display("123 MAIN ST, TACOMA, WA, 98401-1234")
        assert out["state"] == "WA" and out["zip"] == "98401-1234" and out["city"] == "TACOMA"

    def test_two_part_city_only(self):
        out = parse_property_for_display("123 MAIN ST, SEATTLE")
        assert out["street"] == "123 MAIN ST" and out["city"] == "SEATTLE"
        assert out["state"] is None and out["zip"] is None

    def test_two_part_city_state_zip(self):
        out = parse_property_for_display("123 MAIN ST, SEATTLE WA 98101")
        assert out == {"street": "123 MAIN ST", "city": "SEATTLE", "state": "WA", "zip": "98101"}

    def test_no_comma_valid_tail(self):
        # No comma -> state+zip are confident; city is unknowable so it stays blank
        # and the whole pre-state chunk is the street (honest, not a wrong guess).
        out = parse_property_for_display("123 MAIN ST SEATTLE WA 98101")
        assert out["state"] == "WA" and out["zip"] == "98101"
        assert out["street"] == "123 MAIN ST SEATTLE" and out["city"] is None

    def test_no_comma_invalid_state_stays_street_only(self):
        # 'XX' is not a real state code -> do NOT split out a bogus state column.
        out = parse_property_for_display("123 MAIN ST SOMETOWN XX 98101")
        assert out["street"] == "123 MAIN ST SOMETOWN XX 98101"
        assert out["state"] is None and out["city"] is None and out["zip"] is None

    def test_street_only(self):
        out = parse_property_for_display("123 MAIN ST")
        assert out["street"] == "123 MAIN ST"
        assert out["city"] is None and out["state"] is None and out["zip"] is None

    def test_bad_state_not_emitted(self):
        # 'ZZ' isn't a real state code -> never emit it as state (Codex principle).
        out = parse_property_for_display("123 MAIN ST, TACOMA, ZZ 98401")
        assert out["state"] is None

    def test_unit_fragment_not_city(self):
        # '123 MAIN ST, APT 4' -> APT 4 is a unit, NOT a city (Codex P1).
        out = parse_property_for_display("123 MAIN ST, APT 4")
        assert out["city"] is None
        assert "APT 4" in out["street"]

    def test_unit_between_street_and_city(self):
        # '..., UNIT 2, SEATTLE, WA 98101' -> city SEATTLE, unit folds into street.
        out = parse_property_for_display("123 MAIN ST, UNIT 2, SEATTLE, WA 98101")
        assert out["city"] == "SEATTLE" and out["state"] == "WA" and out["zip"] == "98101"
        assert "UNIT 2" in out["street"]

    def test_empty_and_none(self):
        assert parse_property_for_display(None)["street"] is None
        assert parse_property_for_display("")["street"] is None
