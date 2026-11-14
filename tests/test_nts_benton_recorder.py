"""Benton recorded NTS parser, pinned against the text layer of 13 real recorded notices
(Benton County Auditor, recorded 09/29-11/09/2026; fixtures are pypdf's extraction of
each recorder PDF). The expectations below were each checked by reading the notice.
"""
import gzip
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.scrapers.sources.nts_benton_recorder import benton_parcel, parse_recorded_nts

_DIR = Path(__file__).parent / "fixtures" / "benton_nts"


def _parse(name: str) -> dict:
    return parse_recorded_nts(gzip.decompress((_DIR / f"{name}.txt.gz").read_bytes()).decode("utf-8"))


def test_every_notice_yields_its_parcel_address_and_section_iv_principal():
    files = sorted(_DIR.glob("*.txt.gz"))
    assert len(files) == 18
    for f in files:
        p = _parse(f.name[:-7])
        assert p["instrument_number"] and p["notice_code"] == "NTS", f.name
        assert p["parcels"] and all(len(x) == 15 for x in p["parcels"]), f.name
        assert p["grantor"], f.name
        # 2026-019264 prints "Commonly known as: 59111 E 95 PR SE" with no city or zip.
        assert p["property_address"] or f.name.startswith("2026_019264"), f.name
        assert p["principal_owing"] is not None and p["principal_owing_basis"] == "principal", f.name


def test_a_clean_residential_notice():
    p = _parse("2026_025237")
    assert p["instrument_number"] == "2026-025237"
    assert p["recorded_date"] == date(2026, 10, 2)
    assert p["ts_number"] == "WA05000075-26-1"
    assert p["parcels"] == ["110984020208009"]
    assert p["auction_date"] == date(2027, 2, 5) and p["auction_time"] == "01:00PM"
    assert p["principal_owing"] == Decimal("212407.68")
    assert p["property_address"] == "713 SMITH AVE, RICHLAND ,WA 99352"
    assert p["grantor"] == "TRACY RAE PALMER, AN UNMARRIED PERSON"
    assert p["multi_property"] is False and p["commercial"] is False


def test_numeric_sale_date_and_dashed_parcel():
    p = _parse("2026_027262")  # "will on March 12,2027"; parcel printed 1-0189-102-0008-011
    assert p["parcels"] == ["101891020008011"]
    assert p["auction_date"] == date(2027, 3, 12)
    p = _parse("2026_025590")  # "will on 2/12/2027, at 10:00AM"
    assert p["auction_date"] == date(2027, 2, 12) and p["auction_time"] == "10:00AM"


def test_a_later_date_in_the_notice_is_not_the_sale():
    """2026-027553's notary block says 'on October 29, 2026'; the sale is 2/26/2027."""
    assert _parse("2026_027553")["auction_date"] == date(2027, 2, 26)


@pytest.mark.parametrize("name, sale", [
    ("2026_024803", date(2027, 2, 5)),   # Section I "21512027"; Section V "encumbranceson 2/5/2027"
    ("2026_025258", date(2027, 2, 5)),   # Section I "FebruaryS,2027"; Section V "February5,2027"
    ("2026_028388", date(2027, 4, 2)),   # Section I "41212027"
    ("2026_019264", date(2026, 12, 4)),  # Section I "12/4/202610:00AM" runs the time into the year
])
def test_an_unreadable_sale_date_is_read_from_its_section_v_restatement(name, sale):
    assert _parse(name)["auction_date"] == sale


def test_a_sale_date_unreadable_in_both_places_is_left_unparsed_not_guessed():
    # Section I "21512027" and Section V "215/2027": no readable copy, no date.
    assert _parse("2026_025119")["auction_date"] is None


def test_two_readable_sale_dates_that_disagree_give_no_date():
    text = gzip.decompress((_DIR / "2026_025237.txt.gz").read_bytes()).decode("utf-8")
    p = parse_recorded_nts(text)
    assert p["auction_date"] == date(2027, 2, 5)
    restated = text.replace("encumbranceson February5,2027", "encumbranceson February6,2027")
    assert restated != text
    assert parse_recorded_nts(restated)["auction_date"] is None


def test_the_street_and_city_lines_are_joined():
    assert _parse("2026_024803")["property_address"] == "6025W OKANOGAN LOOP, KENNEWICK, WASHINGTON 99336"


def test_grantor_label_variants():
    assert _parse("2026_025590")["grantor"].startswith("TIMOTHY MCINTOSH AND MANNA MCINTOSH")  # "for Recording Purposes"
    assert _parse("2026_027262")["grantor"] == "LuisA.Perez"  # "Grantor(s) of Deed of Trust <name>"
    assert _parse("2026_027155")["grantor"] == "LOVELY J.RANGEL, AN UNMARRIED WOMAN"
    # "Grantor CANDICE MARIE JACKSON, ..." with no colon.
    assert _parse("2026_020482")["grantor"] == "CANDICE MARIE JACKSON, AN UNMARRIED WOMAN"


@pytest.mark.parametrize("name, owner", [
    # Cover sheet "Grantor(s):U.S.Bank NationalAssociation" (the beneficiary); page 2 names the borrower.
    ("2026_022125", "Alvin L Whitish a single man"),
    # Cover sheet "Grantor(s): WellsFargoBank,N.A." (the beneficiary).
    ("2026_014784", "Glen R. Ziegler and wife Donna Ziegler"),
    # Cover sheet "Grantor: EisenhowerCarlsonPLLC" (the trustee, its label wrapped over two lines);
    # the borrower is only in "from SyedJ.Amir, as Grantor".
    ("2026_023349", "SyedJ.Amir"),
])
def test_a_grantor_that_is_the_trustee_or_lender_is_not_the_owner(name, owner):
    assert _parse(name)["grantor"] == owner


def test_a_notice_for_property_in_two_counties_is_flagged_not_pinned():
    p = _parse("2026_027018")  # Benton + Walla Walla parcels in one notice
    assert p["multi_property"] is True and p["other_counties"] == ["Walla Walla"]
    assert p["parcels"] == ["118803050002002"]


def test_benton_parcel_shape():
    assert benton_parcel("1-1880-305-0002-002") == "118803050002002"
    assert benton_parcel("24173") is None           # a PACS property id printed beside it
    assert benton_parcel("30-08-02-53-0701") is None  # a Walla Walla parcel


def test_beneficiary_label_is_consumed_through_its_colon():
    # "...of the Deed of Trust and Grantee (for Recording Purposes under RCW 65.04.015):"
    for name in ("2026_025590", "2026_025706"):
        b = _parse(name)["beneficiary"]
        assert "Grantee" not in b and "RCW" not in b, b


def test_a_beneficiary_list_is_read_through_its_continuation_lines():
    b = _parse("2026_027018")["beneficiary"]
    assert not b.startswith("(ies)")
    assert "CrystalRock Capital" in b and "Terry" in b


def test_a_parcel_list_on_one_label_is_multi_property():
    """Codex: 'ParcelNumber(s): A,B' used to keep only A, so the notice looked single."""
    text = gzip.decompress((_DIR / "2026_025237.txt.gz").read_bytes()).decode("utf-8")
    text = text.replace("ParcelNumber 110984020208009", "ParcelNumber(s): 110984020208009,128993050001007")
    p = parse_recorded_nts(text)
    assert p["parcels"] == ["110984020208009", "128993050001007"] and p["multi_property"] is True


def test_a_parcel_list_continued_on_the_next_line_is_multi_property():
    text = gzip.decompress((_DIR / "2026_025237.txt.gz").read_bytes()).decode("utf-8")
    text = text.replace("ParcelNumber 110984020208009", "ParcelNumber(s): 110984020208009,\n128993050001007")
    assert parse_recorded_nts(text)["multi_property"] is True


def test_a_space_grouped_parcel_is_read():
    text = gzip.decompress((_DIR / "2026_025237.txt.gz").read_bytes()).decode("utf-8")
    text = text.replace("ParcelNumber 110984020208009", "Parcel Number: 1 1098 402 0208 009")
    assert parse_recorded_nts(text)["parcels"] == ["110984020208009"]


def test_a_blank_line_ends_a_name():
    p = parse_recorded_nts("Current Beneficiary: ACME BANK\n\nNOTICE OF TRUSTEE SALE\nSome text")
    assert p["beneficiary"] == "ACME BANK"


def test_a_dated_single_parcel_notice_becomes_an_auction_row():
    from src.scrapers.sources.nts_benton_recorder import recorded_notice_to_row

    row = recorded_notice_to_row(_parse("2026_025237"), "https://rec/doc", date(2026, 11, 12))
    assert row["source"] == "benton_recorder" and row["ts_number"] == "REC-2026-025237"
    assert row["county"] == "benton" and row["parcel"] == "110984020208009"
    assert row["auction_date"] == date(2027, 2, 5) and row["is_active"] is True
    assert row["principal_owing"] == Decimal("212407.68") and row["principal_owing_basis"] == "principal"


def test_a_notice_a_discontinuance_names_is_stored_inactive():
    from src.scrapers.sources.nts_benton_recorder import recorded_notice_to_row

    row = recorded_notice_to_row(_parse("2026_025237"), "u", date(2026, 11, 12), frozenset({"2026-025237"}))
    assert row["is_active"] is False


def test_multi_property_and_undated_notices_are_never_auction_rows():
    from src.scrapers.sources.nts_benton_recorder import recorded_notice_to_row

    assert recorded_notice_to_row(_parse("2026_027018"), "u", date(2026, 11, 12)) is None  # two counties
    assert recorded_notice_to_row(_parse("2026_025119"), "u", date(2026, 11, 12)) is None  # no readable date


def _owner(text: str) -> str | None:
    return parse_recorded_nts(text)["grantor"]


def test_a_wrapped_trustee_name_is_still_not_the_owner():
    """Codex: the skip list compared single lines, so a wrapped trustee got through."""
    text = ("Grantor: Example Trustee Services\nLLC\n"
            "Current Trustee: Example Trustee Services\nLLC\n"
            "This Deed of Trust was executed by JANE Q DOE, AN UNMARRIED WOMAN as Trustor(s), to secure\n")
    assert _owner(text) == "JANE Q DOE, AN UNMARRIED WOMAN"


def test_a_colonless_trustee_label_is_still_not_the_owner():
    text = ("Grantor: Affinia Default Services\n"
            "Current Trustee Affinia Default Services\n"
            "This Deed of Trust was executed by JANE Q DOE as Trustor(s)\n")
    assert _owner(text) == "JANE Q DOE"


def test_a_bare_grantor_line_does_not_take_the_next_field():
    """Codex: 'Grantor' alone on its line used to read the next line as the owner."""
    text = "Grantor\nCurrent Trustee Acme Trustee LLC\nCurrent Beneficiary: Big Bank\n"
    assert _owner(text) is None


def test_wrapped_borrower_fallbacks_read_the_whole_name():
    text = ("Grantor: Big Bank NA\nCurrent Beneficiary: Big Bank NA\n"
            "Original Mortgagor(s): Jane Doe and\nJohn Doe\n\nLegal: LOT 1\n")
    assert _owner(text) == "Jane Doe and John Doe"
    text = ("Grantor: Big Bank NA\nCurrent Beneficiary: Big Bank NA\n"
            "recorded with the Auditor (the Deed of Trust), from Jane Doe and\nJohn Doe, as Grantor, to\n")
    assert _owner(text) == "Jane Doe and John Doe"
