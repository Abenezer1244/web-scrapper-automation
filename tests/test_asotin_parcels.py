"""Tying an Asotin probate decedent to parcels (owner decision 2026-11-14: exact name AND
a family tie; everything else held).

The layer fixture is a real response of the county's public parcel layer (names and
billing addresses changed). Variants of a real row are made in the test with
dict(row, NAME=...) so each rule is pinned on the real field layout.
"""
import json
from pathlib import Path

from src.scrapers.enrichment.asotin_parcels import (
    Person,
    mailing_of,
    match_estate,
    notice_person,
    owner_people,
)

_ROWS = [f["attributes"] for f in json.loads(
    (Path(__file__).parent / "fixtures" / "asotin_parcel_layer_owners.json").read_text())["features"]]


def _row(name_prefix: str) -> dict:
    return next(r for r in _ROWS if r["NAME"].startswith(name_prefix))


# ── names ────────────────────────────────────────────────────────────────────

def test_owner_name_layouts():
    assert owner_people("TILLMAN, MARTIN S & MELODY S") == [
        Person("MARTIN", "S", "TILLMAN", ""), Person("MELODY", "S", "TILLMAN", "")]
    assert owner_people("WARD JR, ROGER A & TARA D") == [
        Person("ROGER", "A", "WARD", "JR"), Person("TARA", "D", "WARD", "")]
    assert owner_people("HERRERA, JR, NOEL") == [Person("NOEL", "", "HERRERA", "JR")]
    assert owner_people("OLSEN, TOM J JR") == [Person("TOM", "J", "OLSEN", "JR")]
    assert owner_people("FINCH, RONALD JOSEPH & DAVIS, DONNA") == [
        Person("RONALD", "JOSEPH", "FINCH", ""), Person("DONNA", "", "DAVIS", "")]
    assert owner_people("WEBER, NORMA E ESTATE C/O GLENN L. WEBER PR") == [
        Person("NORMA", "E", "WEBER", "")]
    assert owner_people("ASHFORD, EDITH (ESTATE)") == [Person("EDITH", "", "ASHFORD", "")]
    assert owner_people("WAL- MART REAL ESTATE BUSINESS TRUST") == []


def test_notice_names():
    assert notice_person("MARGARET J. HOLLOWAY") == Person("MARGARET", "J", "HOLLOWAY", "")
    assert notice_person("JOHN A. DOE, JR.") == Person("JOHN", "A", "DOE", "JR")
    assert notice_person("CHER") is None


def test_mailing_skips_addressee_lines():
    assert mailing_of(_row("OLSEN")) == "400 12TH ST, CLARKSTON, WA 99403"
    assert mailing_of(_row("WAL- MART")) == "PO BOX 8050, MS 0555, BENTONVILLE, AR 72716"
    assert mailing_of(dict(_row("HOLLOWAY"), ZIP="")) is None


# ── the match rule ───────────────────────────────────────────────────────────

def test_rep_sharing_surname_ties_the_parcel():
    m = match_estate("MARGARET J. HOLLOWAY", ("RACHEL M. DUNN", "AMANDA J. HOLLOWAY"),
                     "P.O. Box 100 Clarkston, WA 99403", _ROWS)
    assert [lead.parcel_id for lead in m.leads] == ["10412300600050000"]
    lead = m.leads[0]
    assert lead.ties == ("rep_shares_surname",)
    assert lead.property_address == "2300 RIVERVIEW BLVD"
    assert lead.mailing_address == "2300 RIVERVIEW BLVD, CLARKSTON, WA 99403"


def test_exact_name_without_a_tie_is_held():
    m = match_estate("MARGARET J. HOLLOWAY", ("RACHEL M. DUNN",), None, _ROWS)
    assert m.leads == [] and m.name_matched == 1
    assert m.held == ["no_family_tie"]


def test_estate_marked_owner_with_rep_named_on_parcel():
    m = match_estate("NORMA E. WEBER", ("GLENN L. WEBER",), None, _ROWS)
    assert m.leads[0].ties == ("estate_marked", "rep_on_parcel", "rep_shares_surname")
    assert m.leads[0].mailing_address == "29000 N HILL RD, CHATTAROY, WA 99003"


def test_estate_marked_owner_alone_is_a_tie_and_parcels_without_situs_are_held():
    m = match_estate("EDITH ASHFORD", ("SAM K. LEE",), None, _ROWS)
    assert [lead.parcel_id for lead in m.leads] == ["11320003200000000"]
    assert m.leads[0].ties == ("estate_marked",)
    assert m.held == ["missing_property_address"]


def test_life_estate_is_not_an_estate():
    m = match_estate("TOM J. OLSEN, JR.", ("SAM K. LEE",), None, _ROWS)
    assert m.name_matched == 1 and m.leads == [] and m.held == ["no_family_tie"]


def test_billing_equal_to_claims_address_is_a_tie():
    m = match_estate("NOEL HERRERA, JR.", ("SAM K. LEE",), "401 Elm St, Clarkston, WA 99403", _ROWS)
    assert m.leads[0].ties == ("billing_is_claims_address",)


def test_middle_initial_given_must_agree():
    reps = ("AMANDA J. HOLLOWAY",)
    assert match_estate("MARGARET A. HOLLOWAY", reps, None, _ROWS).name_matched == 0
    assert match_estate("MARGARET JANE HOLLOWAY", reps, None, _ROWS).name_matched == 1
    # The notice gave no middle: the record's middle initial is the same name.
    assert match_estate("MARGARET HOLLOWAY", reps, None, _ROWS).name_matched == 1
    # The notice gave one and the record has none: not exact.
    rows = [dict(_row("HOLLOWAY"), NAME="HOLLOWAY, MARGARET")]
    assert match_estate("MARGARET J. HOLLOWAY", reps, None, rows).name_matched == 0


def test_suffix_must_agree():
    reps = ("TARA D. WARD",)
    assert match_estate("ROGER A. WARD", reps, None, _ROWS).name_matched == 0
    assert match_estate("ROGER A. WARD, JR.", reps, None, _ROWS).name_matched == 2


def test_co_owner_is_matched_and_every_parcel_is_a_lead():
    m = match_estate("ROGER A. WARD, JR.", ("TARA D. WARD",), None, _ROWS)
    assert sorted(lead.parcel_id for lead in m.leads) == ["13560000100000000", "60010502700000000"]
    # the spouse named after "&" is matched with the shared surname
    assert match_estate("TARA D. WARD", ("ROGER A. WARD",), None, _ROWS).name_matched == 2


def test_parcels_billed_to_different_addresses_are_held():
    rows = [_row("HOLLOWAY"), dict(_row("HOLLOWAY"), PARCEL_ID="10412300600060000",
                                   BILLING1="PO BOX 9", CITY="SPOKANE", ZIP="99201")]
    m = match_estate("MARGARET J. HOLLOWAY", ("AMANDA J. HOLLOWAY",), None, rows)
    assert m.leads == [] and m.held == ["matched_parcels_billed_to_different_addresses"]


def test_no_parcel_and_prefix_names_are_held():
    assert match_estate("DOROTHY L. PRATT", ("CYNTHIA J. MOORE",), None, _ROWS).held == [
        "no_parcel_in_decedents_name"]
    # "MARGARET J. HOLLOW" is a prefix of the owner's surname, not the owner.
    assert match_estate("MARGARET J. HOLLOW", ("AMANDA J. HOLLOW",), None, _ROWS).name_matched == 0
