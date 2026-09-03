"""Regression: compose_situs must not give a line a second copy of its own tail.

The seam between PR #187 (King assessor enrichment) and PR #188 (migration 085
structured situs). #187's King "Site Address" carries its own trailing ZIP and
#188's backfill parses parts straight out of property_address — both then hand
that same full line to compute_owner_flags. The original append-only compose
produced "… 4C 98023, 98023", which pushed the ZIP into the parsed STREET so the
street stopped matching the mailing street and absentee_owner flipped
False -> True on owner-occupied leads. Measured, not hypothetical.
"""

from src.utils.address_intel import compose_situs, compute_owner_flags


def test_king_site_address_with_trailing_zip_is_not_doubled():
    # PR #187 King path: the ZIP lives inside property_address AND in property_zip.
    prop = "2019 SW 318TH PL 4C 98023"
    assert compose_situs(prop, None, None, "98023") == "2019 SW 318TH PL 4C, 98023"


def test_king_owner_occupied_is_not_flagged_absentee():
    flags = compute_owner_flags(
        "2019 SW 318TH PL 4C 98023",
        "2019 SW 318TH PL 4C, FEDERAL WAY, WA 98023",
        property_zip="98023",
    )
    assert flags["absentee_owner"] is False


def test_full_situs_line_with_all_parts_is_unchanged():
    # backfill_property_situs_parts.py shape: parts parsed out of the line itself.
    line = "123 MAIN ST, TACOMA, WA 98402"
    assert compose_situs(line, "TACOMA", "WA", "98402") == line
    flags = compute_owner_flags(line, line, property_city="TACOMA",
                                property_state="WA", property_zip="98402")
    assert flags["absentee_owner"] is False


def test_street_only_line_still_gains_its_parts():
    # PR #188's INTENDED win must survive: None -> confirmed False.
    assert compose_situs("123 MAIN ST", "TACOMA", "WA", "98402") == "123 MAIN ST, TACOMA, WA 98402"
    assert compute_owner_flags("123 MAIN ST", "123 MAIN ST, TACOMA, WA 98402")["absentee_owner"] is None
    assert compute_owner_flags(
        "123 MAIN ST", "123 MAIN ST, TACOMA, WA 98402",
        property_city="TACOMA", property_state="WA", property_zip="98402",
    )["absentee_owner"] is False


def test_a_real_absentee_is_still_true():
    flags = compute_owner_flags("123 MAIN ST", "999 FAR RD, SEATTLE, WA 98101",
                                property_city="TACOMA", property_state="WA", property_zip="98402")
    assert flags["absentee_owner"] is True


def test_line_wins_over_a_conflicting_part():
    # Two real sources disagreeing is not licence to invent "…99999, TACOMA, WA 98402".
    assert compose_situs("123 MAIN ST, TACOMA, WA 99999", "TACOMA", "WA", "98402") == (
        "123 MAIN ST, TACOMA, WA 99999"
    )


def test_only_a_validated_tail_counts_as_already_present():
    # A leading 5-digit house number is not a ZIP; a street word is not a city.
    assert compose_situs("98023 MAIN ST", None, None, "98023") == "98023 MAIN ST, 98023"
    assert compose_situs("123 LAKE ST", "LAKE") == "123 LAKE ST, LAKE"


def test_compose_is_idempotent():
    once = compose_situs("2019 SW 318TH PL 4C 98023", None, None, "98023")
    assert compose_situs(once, None, None, "98023") == once
