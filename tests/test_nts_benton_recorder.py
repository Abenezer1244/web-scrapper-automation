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
    assert len(files) == 13
    for f in files:
        p = _parse(f.name[:-7])
        assert p["instrument_number"] and p["notice_code"] == "NTS", f.name
        assert p["parcels"] and all(len(x) == 15 for x in p["parcels"]), f.name
        assert p["property_address"], f.name
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


@pytest.mark.parametrize("name", ["2026_024803", "2026_025119", "2026_025258", "2026_028388"])
def test_an_ocr_damaged_sale_date_is_left_unparsed_not_guessed(name):
    # "21512027" (2/5/2027 with slashes read as 1), "FebruaryS,2027", "41212027".
    assert _parse(name)["auction_date"] is None


def test_the_street_and_city_lines_are_joined():
    assert _parse("2026_024803")["property_address"] == "6025W OKANOGAN LOOP, KENNEWICK, WASHINGTON 99336"


def test_grantor_label_variants():
    assert _parse("2026_025590")["grantor"].startswith("TIMOTHY MCINTOSH AND MANNA MCINTOSH")  # "for Recording Purposes"
    assert _parse("2026_027262")["grantor"] == "LuisA.Perez"  # "Grantor(s) of Deed of Trust <name>"
    assert _parse("2026_027155")["grantor"] == "LOVELY J.RANGEL, AN UNMARRIED WOMAN"


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
    assert recorded_notice_to_row(_parse("2026_024803"), "u", date(2026, 11, 12)) is None  # OCR-damaged date
