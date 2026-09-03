"""A placeholder street must not manufacture a confident absentee_owner.

The Snohomish tax bulk file encodes "no situs on file" as the literal word
UNKNOWN rather than as a blank column, which walks straight through the
emptiness-based _has_street guard. 'UNKNOWN UNKNOWN' can never equal a real
mailing street, so every such row became absentee_owner=True — a confident
user-facing claim about a property whose address we do not have.

Measured in production 2026-09-03: 408 rows, all snohomish/tax_delinquent, all
absentee_owner=True; the entire street segment is UNKNOWN tokens in every one,
and 0 rows carry UNKNOWN inside an otherwise real street.
"""

from src.utils.address_intel import compute_owner_flags, street_is_placeholder

_REAL_MAILING = "1234 REAL ST, EVERETT, WA 98208"


def test_detects_the_measured_placeholder_shapes():
    assert street_is_placeholder("UNKNOWN, SNOHOMISH WA") is True
    assert street_is_placeholder("UNKNOWN UNKNOWN, LAKE STEVENS WA 98258") is True
    assert street_is_placeholder("UNKNOWN UNKNOWN UNKNOWN, GRANITE FALLS WA 98252") is True


def test_a_real_street_is_never_a_placeholder():
    # 0 production rows look like this today, but the guard must not be able to
    # swallow one if a county ever ships it.
    assert street_is_placeholder("123 UNKNOWN RD, TACOMA, WA 98402") is False
    assert street_is_placeholder("8822 2ND AVE S") is False
    assert street_is_placeholder(None) is False
    assert street_is_placeholder("") is False


def test_placeholder_situs_yields_unknown_not_absentee():
    flags = compute_owner_flags("UNKNOWN UNKNOWN, GRANITE FALLS WA 98252", _REAL_MAILING)
    assert flags["absentee_owner"] is None


def test_the_real_locality_is_still_used():
    # Only the STREET is missing. City/state/zip in that line are REAL, so the
    # flags that depend on locality rather than on the street must survive.
    flags = compute_owner_flags("UNKNOWN UNKNOWN, GRANITE FALLS WA 98252", _REAL_MAILING)
    assert flags["property_state"] == "WA"
    assert flags["owner_state"] == "WA"
    assert flags["out_of_state_owner"] is False


def test_out_of_state_still_detected_through_a_placeholder_street():
    flags = compute_owner_flags("UNKNOWN UNKNOWN, GRANITE FALLS WA 98252",
                                "500 MAIN ST, HOOD RIVER, OR 97031")
    assert flags["out_of_state_owner"] is True
    assert flags["absentee_owner"] is None


def test_a_placeholder_mailing_street_is_also_unknown():
    flags = compute_owner_flags("8822 2ND AVE S, SEATTLE, WA 98108", "UNKNOWN, SEATTLE WA 98108")
    assert flags["absentee_owner"] is None


def test_normal_rows_are_unaffected():
    assert compute_owner_flags("123 MAIN ST, TACOMA, WA 98402",
                               "999 FAR RD, SEATTLE, WA 98101")["absentee_owner"] is True
    assert compute_owner_flags("123 MAIN ST, TACOMA, WA 98402",
                               "123 MAIN ST, TACOMA, WA 98402")["absentee_owner"] is False


def test_punctuated_placeholder_debris_is_still_a_placeholder():
    """Splitting on whitespace alone let these through and kept a fake absentee."""
    from src.utils.address_intel import street_is_placeholder
    for street in ("UNKNOWN.", "UNKNOWN-UNKNOWN", "UNKNOWN/UNKNOWN", "UNKNOWN,UNKNOWN",
                   "unknown", "  UNKNOWN   UNKNOWN  ", "UNKNOWN / UNKNOWN"):
        assert street_is_placeholder(f"{street}, GRANITE FALLS WA 98252") is True, street


def test_a_real_street_containing_the_word_is_never_suppressed():
    """The guard must not cost us a real lead — all() still requires every token."""
    from src.utils.address_intel import street_is_placeholder
    for street in ("123 UNKNOWN RD", "UNKNOWN 123 MAIN", "1 UNKNOWNVILLE DR",
                   "500 UNKNOWN CREEK LN"):
        assert street_is_placeholder(f"{street}, EVERETT WA 98204") is False, street
