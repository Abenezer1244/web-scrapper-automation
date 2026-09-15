"""Tests for dialer-CSV display formatting (src/utils/lead_formatting.py)."""
import pytest

from src.utils.lead_formatting import (
    _COOWNER_JOIN_RE,
    NAME_ORDER_COMMA_ONLY,
    NAME_ORDER_NATURAL,
    NAME_ORDER_RECORDER,
    classify_probate_title_status,
    normalize_phone_for_dialer,
    parse_property_for_display,
    split_first_person,
    split_owner_for_display,
)


class TestClassifyProbateTitleStatus:
    """Conservative probate current-owner vs deceased party_name classifier. Real
    cases from the King "king pro tax" audit (2026-07-03)."""

    def test_same_person_abbreviated_first_name_not_flagged(self):
        # BOUCHER CHARLES DENNIS vs Assessor "BOUCHER CHARLES D" — same person.
        assert classify_probate_title_status("BOUCHER CHARLES DENNIS", "BOUCHER CHARLES D") == ""

    def test_same_owner_unchanged_not_flagged(self):
        assert classify_probate_title_status("HOBI MICHAEL E", "HOBI MICHAEL E") == ""

    def test_different_surname_flags_name_differs(self):
        # HOWTON JAMES W -> Assessor now "LEAPAI MICHELLE".
        assert (
            classify_probate_title_status("HOWTON JAMES W", "LEAPAI MICHELLE")
            == "current_owner_name_differs"
        )

    def test_hyphenated_new_surname_flags_differs(self):
        # JONES JAMES EDWARD JR -> "JONES-MITCHELL NESHELLA": a NEW party, must flag.
        assert (
            classify_probate_title_status("JONES JAMES EDWARD JR", "JONES-MITCHELL NESHELLA")
            == "current_owner_name_differs"
        )

    def test_trust_owner_flags_entity_even_with_matching_surname(self):
        # PRYOR JOYCE JOANNE -> "PRYOR TRUST MARK A": entity wins over surname match.
        assert (
            classify_probate_title_status("PRYOR JOYCE JOANNE", "PRYOR TRUST MARK A")
            == "current_owner_entity_or_trust"
        )

    def test_co_owner_survivor_shared_surname_not_flagged(self):
        # King '+'-joined co-owners sharing a surname: the deceased's surname is still
        # present -> not a transfer.
        assert classify_probate_title_status("JANNETTO RUSSELL D", "JANNETTO RUSSELL D+GINA L") == ""

    def test_deceased_surname_present_among_multi_owners_not_flagged(self):
        assert classify_probate_title_status("SMITH JOHN", "DOE BOB / SMITH JANE") == ""

    def test_estate_of_owner_keeps_surname_not_flagged(self):
        assert classify_probate_title_status("HOWTON JAMES W", "ESTATE OF JAMES HOWTON") == ""

    def test_blank_owner_returns_blank(self):
        assert classify_probate_title_status("HOWTON JAMES W", "") == ""
        assert classify_probate_title_status("HOWTON JAMES W", None) == ""

    def test_unparseable_deceased_returns_blank(self):
        # No surname to compare (and owner isn't an entity) -> no flag.
        assert classify_probate_title_status("", "LEAPAI MICHELLE") == ""

    def test_surrounding_whitespace_owner_not_misparsed(self):
        # Padded owner must still parse surname HOWTON (not "W") -> same owner, no flag.
        assert classify_probate_title_status("HOWTON JAMES W", "  HOWTON JAMES W  ") == ""


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


class TestSplitFirstPerson:
    """Source-aware First/Last for the export. Cases are real prod party_name shapes
    (read-only sample 2026-09-14) plus the owner-supplied edge cases."""

    @pytest.mark.parametrize("name, expected", [
        ("SMITH JOHN", ("JOHN", "SMITH")),
        ("SMITH JOHN J", ("JOHN", "SMITH")),                       # middle initial
        ("HALL MARVIN WAYNE", ("MARVIN", "HALL")),                  # middle name
        ("WEBB JR HAROLD", ("HAROLD", "WEBB")),                     # suffix is not a first name
        ("LAMB GILBERT C III", ("GILBERT", "LAMB")),
        ("JOHNSON WILLIAM EST OF", ("WILLIAM", "JOHNSON")),         # Pierce probate tail
        ("HAVEN-JOHNSON ANDREA", ("ANDREA", "HAVEN-JOHNSON")),
        ("DE LA CRUZ MARIA", ("MARIA", "DE LA CRUZ")),
        ("CARPENTER, JOHN R", ("JOHN", "CARPENTER")),               # comma form
        # Multiple owners: the FIRST-listed person, whatever the joiner.
        ("PALMER DAVID M / PALMER CAROLYN M", ("DAVID", "PALMER")),
        ("GOSS WESLEY+MARIE", ("WESLEY", "GOSS")),                  # King assessor '+'
        ("CISSNA RICHARD C/KATHRYN A", ("RICHARD", "CISSNA")),      # Snohomish bare '/'
        ("BERG JACQUELINE M & SCATES DARYN T", ("JACQUELINE", "BERG")),
        ("BOYLE DAVID E / QUALITY LOAN SERVICE CORP", ("DAVID", "BOYLE")),
        ("QUALITY LOAN SERVICE CORP / BOYLE DAVID E", ("DAVID", "BOYLE")),  # entity skipped
        # Prod old-vs-new diff (2026-09-14, 163,261 rows) regressions, pinned:
        ("MERCER JOANNE HEIRS OF", ("JOANNE", "MERCER")),           # trailing decedent marker
        ("NEWBURY CLARECE HEIRS OF(+)", ("CLARECE", "NEWBURY")),    # Pierce '(+)' marker
        ("KHURANA H S", (None, "KHURANA")),                         # initials only: surname kept
        ("ESTATE OF SMITH, JOHN", ("JOHN", "SMITH")),  # recorder estate: comma form only
        ("SHANNON JR ROBERT L", ("ROBERT", "SHANNON")),
        ("RATSHIN ANDREW+FIELD,HILARY", ("ANDREW", "RATSHIN")),
        ("LE KHANG & NGUYEN ANH", ("KHANG", "LE")),
    ])
    def test_recorder_order(self, name, expected):
        assert split_first_person(name, NAME_ORDER_RECORDER) == expected

    @pytest.mark.parametrize("name, expected", [
        ("SHIRLEY A JOHNSON", ("SHIRLEY", "JOHNSON")),   # was first='A', last='SHIRLEY'
        ("JOHN J SMITH", ("JOHN", "SMITH")),
        ("MICHAEL P. BYRD", ("MICHAEL", "BYRD")),
        ("JOHN SMITH JR", ("JOHN", "SMITH")),
        ("MARY VAN DYKE", ("MARY", "VAN DYKE")),
        ("Julie Anderson", ("Julie", "Anderson")),        # case preserved, never invented
        ("MARCUS ALLEYNE AND KAELYN ALLEYNE", ("MARCUS", "ALLEYNE")),
        ("THOMAS D. ROLFZEN, A SINGLE INDIVIDUAL", ("THOMAS", "ROLFZEN")),
        ("TYLER D WARE, AN UNMARRIED INDIVIDUAL AND CHRISTINA N ZAWAIDEH, AN UNMARRIED "
         "INDIVIDUAL", ("TYLER", "WARE")),
        ("Robert B Snider, as a separate estate", ("Robert", "Snider")),
        ("ESTATE OF JOHN SMITH", ("JOHN", "SMITH")),
    ])
    def test_natural_order(self, name, expected):
        assert split_first_person(name, NAME_ORDER_NATURAL) == expected

    @pytest.mark.parametrize("name, order", [
        # Shared surname: 'JOHN' alone is not a full name -> never 'AND'/'JOHN'.
        ("JOHN AND JANE SMITH", NAME_ORDER_NATURAL),
        ("JOHN AND JANE SMITH, HUSBAND AND WIFE", NAME_ORDER_NATURAL),
        ("UNKNOWN HEIRS OF SMITH JOHN", NAME_ORDER_RECORDER),
        ("SMITH JOHN ET AL", NAME_ORDER_RECORDER),
        ("ABC HOLDINGS LLC", NAME_ORDER_RECORDER),
        ("ABC HOLDINGS LLC", NAME_ORDER_NATURAL),
        ("JOHN SMITH REVOCABLE TRUST", NAME_ORDER_NATURAL),
        ("Next Level 3 REI, LLC, a Washington limited liability company", NAME_ORDER_NATURAL),
        # Natural-order comma is a list separator as often as 'LAST, FIRST'.
        ("INGABIRE UQIMANA, JUDITH UMUTONI AND UWIDUHAYE NYIRAMUGISHA", NAME_ORDER_NATURAL),
        ("Lee, Sang Ki and Lee, Hye Kyung", NAME_ORDER_NATURAL),
        ("Nuisance - 3711 S D ST", NAME_ORDER_RECORDER),   # Pierce CV case label
        # An entity inside ONE owner cell must not be cut into a fake person (prod).
        ("WSDOT R/E SERVICES", NAME_ORDER_RECORDER),
        ("HEARTWOOD SPE LLC C/O COMMU", NAME_ORDER_RECORDER),
        ("GLACIER HOA C/O MAGUIRE C", NAME_ORDER_RECORDER),
        ("NU DES & ENGG ROXHILL HOMES", NAME_ORDER_RECORDER),
        ("YESLER TOWERS LLC/CHAN J", NAME_ORDER_RECORDER),
        ("DEPT OF NATURAL RESOURCES", NAME_ORDER_RECORDER),
        ("PETRAKOPOULOS/ALEX AND SHANNON", NAME_ORDER_RECORDER),
        ("LUKINS & ANNIS", NAME_ORDER_RECORDER),
        # Codex review (Phase 1): care-of line, vesting words without their comma.
        ("FOUR M ALLIANCE CORPORATION", NAME_ORDER_RECORDER),
        ("BENSON JR FOOTBALL ASSOC", NAME_ORDER_RECORDER),
        ("MINADOKA L L C", NAME_ORDER_RECORDER),
        ("SAN MARCO L.L.P.", NAME_ORDER_RECORDER),
        # Recorder source writes both orders after 'ESTATE OF' (prod), so blank.
        ("ESTATE OF KLUG DORIS ANN/FRASER DONALD R", NAME_ORDER_RECORDER),
        ("ESTATE OF RICHARD TODD", NAME_ORDER_RECORDER),
        ("LE MAI H", NAME_ORDER_RECORDER),                  # Vietnamese LE, not a particle
        # Recorder 'LAST F MIDDLE' vs leaked natural 'FIRST M LAST': same shape, blank.
        ("STEPHEN P MYERS / ROBBINS GEORGIA A", NAME_ORDER_RECORDER),
        ("LAVENDER A LORENE", NAME_ORDER_RECORDER),
        ("JOHN SMITH HUSBAND AND WIFE", NAME_ORDER_NATURAL),
        ("SMITH JOHN AND JANE MARRIED", NAME_ORDER_RECORDER),
        ("MADONNA", NAME_ORDER_RECORDER),                   # lone token
        ("DAVID A BARTHOLOMEW", NAME_ORDER_COMMA_ONLY),     # mixed source, no comma
        ("SMITH JOHN", None),                               # unknown source order
        ("", NAME_ORDER_RECORDER),
        (None, NAME_ORDER_RECORDER),
    ])
    def test_ambiguous_or_non_person_yields_blank(self, name, order):
        assert split_first_person(name, order) == (None, None)

    # ── 2026-09-15: double surnames, roles, organizations, Vietnamese order ──────
    # Every case below is a real prod party_name from a read-only scan of all 4+-word
    # names (or a Codex adversarial case). Pinned outputs were hand-reviewed.

    @pytest.mark.parametrize("name, expected", [
        ("ALATORRE HERNANDEZ JOSE LUIS", ("JOSE", "ALATORRE HERNANDEZ")),
        ("GUZMAN CAMPOS MARIA F", ("MARIA", "GUZMAN CAMPOS")),
        ("ORELLANA PADILLA ROXANA YOHELI", ("ROXANA", "ORELLANA PADILLA")),
        ("MORALES MONDRAGON JUAN RENE", ("JUAN", "MORALES MONDRAGON")),
        ("GARCIA RAMOS DAVID", ("DAVID", "GARCIA RAMOS")),
        ("DELA CRUZ PEREZ BRANDON A", ("BRANDON", "DELA CRUZ PEREZ")),
        ("RIVERA SANDOVAL ERICK G/BOLAINES CRUZ CI", ("ERICK", "RIVERA SANDOVAL")),
        ("DE LOS SANTOS MARTIN", ("MARTIN", "DE LOS SANTOS")),   # DE LOS binds as a pair
        ("EL SHARAWY KASSAB", ("KASSAB", "EL SHARAWY")),
        ("VASQUEZ LUIS ALBERTO SANTOS", ("LUIS", "VASQUEZ")),    # given name after surname
        ("MARTINEZ LAURA Y JOSE", ("LAURA", "MARTINEZ")),
        ("NGUYEN DIANNA QUYNH THANH", ("DIANNA", "NGUYEN")),     # VN surname in surname slot
        ("DANG CATHY TRAN", ("CATHY", "DANG")),                  # VN surname in 3rd word ok
        ("PHAM DANG", ("DANG", "PHAM")),                          # DANG is a given name too
        ("PHAM ANH THE AND DANG THUY", ("ANH", "PHAM")),         # THE is a VN given name
        ("BAEK JONG HO & KANG EUNJU", ("JONG", "BAEK")),          # Korean HO is not VN
        ("PARK JI HOON", ("JI", "PARK")),                         # PARK surname is not an org
        ("TEMPLE JOHN", ("JOHN", "TEMPLE")),
        ("MEADOWS SARAH K", ("SARAH", "MEADOWS")),
        # Trailing roles are stripped and the person kept (Codex).
        ("ALDRIDGE FAYE MARIE TTEE", ("FAYE", "ALDRIDGE")),
        ("CHINN HING W -TTEE", ("HING", "CHINN")),
        ("BROOKS GENE STEPHEN (TTE)", ("GENE", "BROOKS")),
        ("MEDEIROS ERROL JEREMY (TTEE", ("ERROL", "MEDEIROS")),
        ("ENGLER DAVID M EXEC", ("DAVID", "ENGLER")),
        ("CHIAROLLA DENNIS M SR EXEC(+)", ("DENNIS", "CHIAROLLA")),
        ("GRAY JUDSON PER REP", ("JUDSON", "GRAY")),
        ("GUTSCHMIDT PENNY LEE (ADMN)", ("PENNY", "GUTSCHMIDT")),
        ("NORRIS MARTHA TRUSTEE", ("MARTHA", "NORRIS")),
        ("SMITH JOHN AS TRUSTEE", ("JOHN", "SMITH")),
        ("MENDOZA JOSE M GUILLEN AKA", ("JOSE", "MENDOZA")),       # alias clause cut
        ("BRIGGS JESSE T\\JESSICA RAE", ("JESSE", "BRIGGS")),     # backslash co-owner
    ])
    def test_recorder_surname_runs_and_roles(self, name, expected):
        assert split_first_person(name, NAME_ORDER_RECORDER) == expected

    @pytest.mark.parametrize("name, expected", [
        ("Jessica M. Hernandez Olvera", ("Jessica", "Hernandez Olvera")),
        ("MARIA GARCIA Y LOPEZ", ("MARIA", "GARCIA Y LOPEZ")),
        # A single list word before a non-list surname stays a middle name (Codex).
        ("JOHN RAMOS SMITH", ("JOHN", "SMITH")),
        # Given-name-like surnames are excluded from the list (Codex adversarial).
        ("MARIA LUNA GARCIA", ("MARIA", "GARCIA")),
        ("JOSE SANTIAGO MARTIN", ("JOSE", "MARTIN")),
        ("CARLOS CRUZ LOPEZ", ("CARLOS", "LOPEZ")),
        ("JOHN SMITH AS TRUSTEE", ("JOHN", "SMITH")),
    ])
    def test_natural_double_surnames_and_roles(self, name, expected):
        assert split_first_person(name, NAME_ORDER_NATURAL) == expected

    @pytest.mark.parametrize("name", [
        # Organizations the entity tokens missed (King assessor cells cut at ~27 chars).
        "STATE OF WASHINGTON DNR", "ISLAMIC CENTER OF KENT", "RENTON CHAMBER OF COMMERCE",
        "HEIDEH EFTEHARI LIVING TRUS", "ALKI BEACH REAL ESTATE DEVE",
        "AMERICAN DREAM HOME INVESTM", "PETERSON REAL ESTATE HOLDIN",
        "GRACE POINT NORTHWEST COMMU", "SHILOH MISSIONARY BAPT CH", "UNITED STATES",
        "VIRGINIA ST JOINT VENTURE", "THE MEADOWS AT ROCK CREEK", "HABITAT FOR HUMANITY EKC",
        "RYAN WILLIAM F III REVOCABLE LIVING TRUS", "CHIN FAMILY",
        # Scrambled Vietnamese order: a VN surname in the given-name slot.
        "VU NGUYEN SONG KHANH", "BICH BUI THI NGOC", "DAVID TRAN+NHUNG TRAN",
        # LE opens both French and Vietnamese cells: no way to tell, so blank.
        "LE HOAI NU MINH", "LE THANG HUYNH MINH TRANG", "LE BAUGH CHRISTOPHER MAX",
        # Three surname-looking words, or a double surname followed only by an initial.
        "BULFRANO RAMOS BAEZ MARTINE", "HOLLAND RODRIGUEZ J",
        # 'Mrs. Carl Lange': the given name is not this person's.
        "LANGE CARL R MRS",
        # A role glued to the name is not stripped, so it still blanks.
        "RITA HSIU-HUI KAO-TRUSTEE",
    ])
    def test_organizations_scrambles_and_ambiguous_blank(self, name):
        assert split_first_person(name, NAME_ORDER_RECORDER) == (None, None)

    def test_care_of_is_not_a_co_owner(self):
        assert split_first_person("JOHN SMITH C/O JANE DOE", NAME_ORDER_NATURAL) == ("JOHN", "SMITH")
        assert split_first_person("SMITH JOHN CARE OF DOE JANE", NAME_ORDER_RECORDER) == (
            "JOHN", "SMITH")

    @pytest.mark.parametrize("name, order, expected", [
        ("A JOHNSON", NAME_ORDER_NATURAL, (None, "JOHNSON")),
        ("CHAN J", NAME_ORDER_RECORDER, (None, "CHAN")),
        ("SMITH, A", NAME_ORDER_RECORDER, (None, "SMITH")),  # not a vesting clause (Codex)
        ("JOHNSON, A B", NAME_ORDER_RECORDER, (None, None)),  # ', A ...' reads as vesting: blank
    ])
    def test_bare_initial_is_not_a_first_name(self, name, order, expected):
        assert split_first_person(name, order) == expected

    def test_unspaced_ampersand_splits_co_owners(self):
        assert split_first_person("JOHN SMITH&JANE DOE", NAME_ORDER_NATURAL) == ("JOHN", "SMITH")

    @pytest.mark.parametrize("text, parts", [
        ("AT&T", ["AT&T"]),
        ("B&B", ["B&B"]),
        ("SMITH&JANE", ["SMITH", "JANE"]),
        ("SMITH & JONES", ["SMITH", "JONES"]),
    ])
    def test_ampersand_joiner_skips_short_brand_names(self, text, parts):
        assert _COOWNER_JOIN_RE.split(text) == parts

    def test_comma_only_source_reads_comma_and_estate_forms(self):
        assert split_first_person(
            "CHASE, JUSTIN / ESTATE OF DAYLA JO CHASE", NAME_ORDER_COMMA_ONLY
        ) == ("JUSTIN", "CHASE")
        assert split_first_person(
            "ESTATE OF GLENNA K JONES / JONES, GLENNA K", NAME_ORDER_COMMA_ONLY
        ) == ("GLENNA", "JONES")


class TestParseAddressNonUsAndPlaceholders:
    def test_canadian_multiline_not_forced_into_us_schema(self):
        addr = "716-42 WESTERN BATTERY RD\nTORONTO ON\nM6K3P1\nCANADA"
        assert parse_property_for_display(addr) == {
            "street": addr, "city": None, "state": None, "zip": None,
        }

    def test_canadian_comma_country_never_becomes_city(self):
        out = parse_property_for_display("716-42 WESTERN BATTERY RD, TORONTO ON M6K3P1, CANADA")
        assert out["city"] is None and out["state"] is None and out["zip"] is None
        assert out["street"] == "716-42 WESTERN BATTERY RD, TORONTO ON M6K3P1, CANADA"

    def test_canadian_postal_code_without_country_name(self):
        out = parse_property_for_display("100 KING ST W, TORONTO, ON M5X 1A9")
        assert out["city"] is None and out["zip"] is None and out["state"] is None

    def test_uk_country_tail(self):
        out = parse_property_for_display("10 DOWNING ST, LONDON, UNITED KINGDOM")
        assert out["city"] is None and out["state"] is None

    def test_trailing_usa_is_dropped_and_us_tail_parses(self):
        assert parse_property_for_display("123 MAIN ST, SEATTLE, WA 98101, USA") == {
            "street": "123 MAIN ST", "city": "SEATTLE", "state": "WA", "zip": "98101",
        }

    def test_placeholder_only_address_yields_nothing(self):
        assert parse_property_for_display("UNKNOWN UNKNOWN, UNKNOWN WA") == {
            "street": None, "city": None, "state": None, "zip": None,
        }

    def test_lowercase_placeholder_only_address_yields_nothing(self):
        assert parse_property_for_display("unknown, wa")["street"] is None

    def test_us_unit_shaped_like_canadian_postal_code_still_splits(self):
        assert parse_property_for_display("10 PINE ST UNIT A1B 2C3, SEATTLE, WA 98101") == {
            "street": "10 PINE ST UNIT A1B 2C3", "city": "SEATTLE", "state": "WA", "zip": "98101",
        }

    def test_single_chunk_ending_in_country_word_is_not_foreign(self):
        out = parse_property_for_display("123 CANADA")
        assert out["street"] == "123 CANADA"

    def test_province_and_country_in_one_part(self):
        out = parse_property_for_display("123 Main St, Toronto ON CANADA")
        assert out["city"] is None and out["state"] is None

    def test_placeholder_city_not_emitted(self):
        out = parse_property_for_display("123 MAIN ST, UNKNOWN, WA 98101")
        assert out["city"] is None and out["state"] == "WA" and out["zip"] == "98101"

    def test_military_apo(self):
        assert parse_property_for_display("PSC 123 BOX 4, APO, AE 09012") == {
            "street": "PSC 123 BOX 4", "city": "APO", "state": "AE", "zip": "09012",
        }

    @pytest.mark.parametrize("addr, expected", [
        ("126 SW 148TH ST #C100-1, BURIEN, WA 98166",
         {"street": "126 SW 148TH ST #C100-1", "city": "BURIEN", "state": "WA", "zip": "98166"}),
        ("123 MAIN ST APT 4, SEATTLE, WA 98101-1234",
         {"street": "123 MAIN ST APT 4", "city": "SEATTLE", "state": "WA", "zip": "98101-1234"}),
        ("3213 W. WHEELER ST PMB 131, SEATTLE, WA 98199",
         {"street": "3213 W. WHEELER ST PMB 131", "city": "SEATTLE", "state": "WA", "zip": "98199"}),
        ("PO BOX 500, BELLEVUE, WA 98004",
         {"street": "PO BOX 500", "city": "BELLEVUE", "state": "WA", "zip": "98004"}),
        ("1 ELM ST, HOLTSVILLE, NY 00501",
         {"street": "1 ELM ST", "city": "HOLTSVILLE", "state": "NY", "zip": "00501"}),
        ("4002 22ND ST SE, PUYALLUP, WA, 98374-4108",
         {"street": "4002 22ND ST SE", "city": "PUYALLUP", "state": "WA", "zip": "98374-4108"}),
        ("716-42 WESTERN BATTERY RD, SPOKANE, WA 99201",
         {"street": "716-42 WESTERN BATTERY RD", "city": "SPOKANE", "state": "WA", "zip": "99201"}),
    ])
    def test_us_units_boxes_hyphens_zip4_leading_zero(self, addr, expected):
        assert parse_property_for_display(addr) == expected
