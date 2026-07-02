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
        # The trailing bare ZIP still lifts (it validates on its own, 2026-07-01);
        # the unvalidated 'XX' honestly stays in street.
        out = parse_property_for_display("123 MAIN ST SOMETOWN XX 98101")
        assert out["street"] == "123 MAIN ST SOMETOWN XX"
        assert out["state"] is None and out["city"] is None
        assert out["zip"] == "98101"

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

    # ── No-comma fixes (2026-07-01, evidence-backed from prod census) ─────────

    def test_ne_after_street_suffix_is_directional_not_nebraska(self):
        # REAL prod row shape: '6504 108TH AVE NE 98033' is Kirkland WA — 'NE'
        # is a grid directional. Emitting state=NE (Nebraska) corrupts a
        # dialer-authoritative column. street keeps NE; zip lifts; state blank.
        out = parse_property_for_display("6504 108TH AVE NE 98033")
        assert out["street"] == "6504 108TH AVE NE"
        assert out["state"] is None
        assert out["zip"] == "98033"
        assert out["city"] is None

    def test_ne_after_punctuated_suffix(self):
        out = parse_property_for_display("6504 108TH AVE. NE 98033")
        assert out["state"] is None and out["zip"] == "98033"

    def test_real_nebraska_kept(self):
        # Pre-state token OMAHA is not a street suffix -> NE is really Nebraska.
        out = parse_property_for_display("123 MAIN ST OMAHA NE 68102")
        assert out["state"] == "NE" and out["zip"] == "68102"
        assert out["street"] == "123 MAIN ST OMAHA"

    def test_city_only_line_goes_to_city_not_street(self):
        # Snohomish tax mailing bulk file has NO street — 'STANWOOD WA 98292'.
        # A digitless pre-state chunk is a city; street stays blank.
        out = parse_property_for_display("STANWOOD WA 98292")
        assert out == {"street": None, "city": "STANWOOD", "state": "WA", "zip": "98292"}

    def test_city_only_multiword(self):
        out = parse_property_for_display("MOUNT VERNON WA 98273")
        assert out["city"] == "MOUNT VERNON" and out["state"] == "WA" and out["zip"] == "98273"
        assert out["street"] is None

    def test_city_only_nebraska(self):
        out = parse_property_for_display("OMAHA NE 68102")
        assert out["city"] == "OMAHA" and out["state"] == "NE"

    def test_general_delivery_not_a_city(self):
        # Digitless but a postal delivery line -> street, never city.
        out = parse_property_for_display("GENERAL DELIVERY WA 98292")
        assert out["city"] is None
        assert out["street"] == "GENERAL DELIVERY"
        assert out["state"] == "WA" and out["zip"] == "98292"

    def test_trailing_bare_zip_lifted(self):
        # REAL prod shapes (King pre-foreclosure/probate): bare zip, no state.
        out = parse_property_for_display("1420 E PINE ST 98122")
        assert out["street"] == "1420 E PINE ST" and out["zip"] == "98122"
        assert out["state"] is None and out["city"] is None
        out2 = parse_property_for_display("27323 218TH AVE SE 98038")
        assert out2["street"] == "27323 218TH AVE SE" and out2["zip"] == "98038"

    def test_po_box_number_not_read_as_zip(self):
        out = parse_property_for_display("PO BOX 98292")
        assert out["zip"] is None
        assert out["street"] == "PO BOX 98292"

    def test_no_comma_with_digits_still_street(self):
        # Unchanged conservative behavior: digits in the chunk -> street, city blank.
        out = parse_property_for_display("123 MAIN ST SEATTLE WA 98101")
        assert out["street"] == "123 MAIN ST SEATTLE" and out["city"] is None
        assert out["state"] == "WA" and out["zip"] == "98101"
