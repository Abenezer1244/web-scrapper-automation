"""Four REAL Tacoma Daily Index notices the crawler rejected until 2026-09-02.

A cache-coverage audit (40 listing pages vs nts_notices) found these published
trustee sales missing. Each fixture is the saved article text; each test pins the
layout quirk that dropped it. is_valid_nts needs ts_number AND auction_date.
"""
from datetime import date
from pathlib import Path

from src.scrapers.sources.nts_tacoma_index import (
    is_valid_nts,
    notice_to_row,
    parse_nts_notice,
    parse_tacoma_notice,
)

_FX = Path(__file__).parent / "fixtures"


def _parse(name: str) -> dict:
    return parse_tacoma_notice((_FX / name).read_text(encoding="utf-8"))


def test_numeric_date_with_the_hour_of():
    p = _parse("nts_tacoma_hour_of_numeric.txt")
    assert p["ts_number"] == "25-36636"
    assert p["auction_date"] == "9/25/2026"
    assert p["auction_time"] == "10:00 AM"
    assert p["parcel"] == "7092500220"
    assert is_valid_nts(p)
    assert notice_to_row(p, source_url="https://www.tacomadailyindex.com/x/", today=date(2026, 9, 2))["auction_date"] == date(2026, 9, 25)


def test_month_name_date_with_the_hour_of_and_ref_surrogate():
    p = _parse("nts_tacoma_hour_of_monthname.txt")
    assert p["ts_number"] == "REF-202511050207"      # no TS#: deed-reference surrogate
    assert p["auction_date"] == "July 31, 2026"
    assert p["parcel"] == "0220235061"
    assert is_valid_nts(p)


def test_weekday_prefix_and_documents_referenced_deed_ref():
    p = _parse("nts_tacoma_weekday_docs_referenced.txt")
    assert p["auction_date"] == "August 28, 2026"
    # "Instrument Number 202212290190 (Deed of Trust) 202605120147 (Appointment…)":
    # only the number tagged (Deed of Trust) may become the surrogate key.
    assert p["deed_reference"] == "202212290190"
    assert p["ts_number"] == "REF-202212290190"
    assert is_valid_nts(p)


def test_oclock_without_minutes_and_assessors_parcel_label():
    p = _parse("nts_tacoma_oclock_no_minutes.txt")
    assert p["auction_date"] == "August 7, 2026"
    assert p["auction_time"] == "10:00 AM"
    assert p["parcel"] == "766500-0020"
    assert p["ts_number"] == "APN-766500-0020"        # prose "…defaults now…" is no longer a TS#
    assert "Assessor" not in (p["property_address"] or "")
    assert p["property_address"].startswith("11210 SW 91ST AVENUE CT")
    assert is_valid_nts(p)


def test_ts_label_variants_still_match_and_prose_does_not():
    for txt, want in [
        ("T.S. No.: WA-25-1012820-RM will on 1/2/2026", "WA-25-1012820-RM"),
        ("TS No: 26-78387 Title Order", "26-78387"),
        ("T.S.#: 25-1234 Title", "25-1234"),
        ("T.S.No. 25-9999 Title", "25-9999"),
        ("Trustee Sale No. 24-5555 Title", "24-5555"),
        ("Trustee Sale Number: 24-5556 Title", "24-5556"),
    ]:
        assert parse_nts_notice(txt)["ts_number"] == want, txt
    assert parse_nts_notice("the defaults now in arrears and/or other defaults: $1")["ts_number"] is None
    assert parse_nts_notice("costs now due under the note")["ts_number"] is None
