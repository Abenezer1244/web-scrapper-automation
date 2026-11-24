"""Douglas recorded Notices of Trustee's Sale from the recorder's free detail page.

Fixture: the live detail page of Douglas NTS 3286111 (recorded 11/6/2026, captured
2026-11-17; the borrowers' names replaced by PARTY ONE / PARTY TWO). The remark forms
below are the 13 NTS Douglas recorded 08/01-11/15/2026, verbatim apart from trustee
boilerplate, with the date each was recorded.
"""
from datetime import date
from pathlib import Path

import pytest

from src.scrapers.sources import nts_douglas_recorder as d

_FIX = Path(__file__).parent / "fixtures" / "douglas_nts" / "detail_nts_3286111.html"
_HTML = _FIX.read_text(encoding="utf-8")


def test_the_detail_page_gives_the_notice_fields():
    p = d.parse_detail(_HTML)
    assert p["instrument"] == "3286111"
    assert p["record_date"] == date(2026, 11, 6)
    assert p["doc_code"] == "NTS"
    assert p["grantors"] == ["PARTY, ONE", "PARTY, TWO"]
    assert p["grantees"] == ["MIDFIRST BANK", "QUALITY LOAN SERVICE CORPORATION"]
    assert p["related"] == [("3227557", "DT")]
    assert p["apns"] == ["96600001800"]
    assert "WILL ON 3/19/2027" in p["remarks"]


def test_a_page_that_is_not_a_detail_page_is_none():
    assert d.parse_detail("<html><body>Error 5001</body></html>") is None


def test_the_notice_becomes_a_row_keyed_by_its_recording():
    row, why = d.notice_to_row(d.parse_detail(_HTML), "https://x/details/1", date(2026, 11, 17))
    assert why == "ok"
    assert row["ts_number"] == "REC-3286111" and row["source"] == "douglas_recorder"
    assert row["parcel"] == "96600001800"           # string, 11 digits
    assert row["auction_date"] == date(2027, 3, 19)
    assert row["auction_time"] == "9:00 AM"
    assert row["grantor"] == "PARTY, ONE / PARTY, TWO"
    assert row["principal_owing"] is None             # not in the remarks: never guessed
    assert row["is_active"] is True


def test_a_discontinued_notice_is_stored_inactive():
    row, _ = d.notice_to_row(d.parse_detail(_HTML), "u", date(2026, 11, 17),
                             cancelled=frozenset({"3286111"}))
    assert row["is_active"] is False


# (recorded, remarks, expected sale) — the live forms.
_LIVE = [
    ("2026-08-14", "WILL ON 12/28/26, AT 9:00AM, AT THE FRONT ENTRANCE ... SELL AT PUBLIC AUCTION", date(2026, 12, 28)),
    ("2026-08-20", "NOTICE IS HEREBY GIVEN THAT ON DECEMBER 28, 2026, 10:00AM, AT MAIN ENTRANCE ... "
                   "WILL SELL AT PUBLIC AUCTION", date(2026, 12, 28)),
    ("2026-08-26", "WILL ON DECEMBER 4TH 2026, AT 10:00AM, ... SELL AT PUBLIC AUCTION", date(2026, 12, 4)),
    ("2026-09-03", "NOTICE IS HEREBY GIVEN THAT ON FRIDAY DECEMBER 4TH, 2026, AT THE HOUR OF 10:00 O'CLOCK AM, OUTSIDE THE MAIN "
                   "ENTRANCE OF THE CHELAN COUNTY COURTHOUSE ... SELL AT PUBLIC AUCTION", date(2026, 12, 4)),
    ("2026-09-10", "WILL ON JANUARY 22, 2027 AT THE HOR OF 9:00AM ... SELL AT PUBLIC AUCTION", date(2027, 1, 22)),
    ("2026-09-23", "SALE TO HAPPEN JANUARY 29, 2027 AT THE DOUGLAS COUNTY COURTHOUSE WATERVILLE WA AT 10:00AM",
     date(2027, 1, 29)),
    ("2026-09-29", "SALE TO HAPPEN FEBRUARY 5,2027 AT 10:00AM AT 100 19TH ST NW EAST WENATCHEE WA", date(2027, 2, 5)),
    ("2026-10-07", "NOTICE IS HEREBY GIVEN THAT ON JANUARY 8TH, 2027, 9:00AM, ... WILL SELL AT PUBLIC AUCTION", date(2027, 1, 8)),
    ("2026-10-15", "WILL ON FEBRUARY 19, 2027, AT THE HOUR OF 9:00 AM ... SELL AT PUBLIC AUCTION", date(2027, 2, 19)),
    ("2026-11-13", "AT 10:00AM ON 4/2/2027 SALE AT DOUGLAS COUNTY BUILDING 100 19TH ST NW", date(2027, 4, 2)),
]


@pytest.mark.parametrize("recorded,remarks,sale", _LIVE)
def test_every_live_remark_form_gives_its_sale_date(recorded, remarks, sale):
    got, _time, why = d.parse_sale(remarks, date.fromisoformat(recorded))
    assert (got, why) == (sale, "found")


@pytest.mark.parametrize("remarks", [
    "FEBRUARY 5, 2027 FRONT ENTRANCE OF DOUGLAS COUNTY 100 19TH ST NW",   # live: no sale words
    "FEBRUARY 5, 2027 AT 9:00AM DOUGLAS COUNTY ANNEX AT 100 19TH ST NW",  # live: no sale words
])
def test_a_bare_date_without_sale_language_is_held(remarks):
    assert d.parse_sale(remarks, date(2026, 9, 23))[2] == "no_sale_language"


@pytest.mark.parametrize("remarks,why", [
    ("", "no_remarks"),
    ("THE TRUSTEE WILL SELL AT THE ANNEX", "no_date"),
    ("DEED OF TRUST RECORDED 3/1/2019; WILL ON 2/5/2027 SELL", "several_dates"),
    ("WILL ON 2/4/2027 SELL AT PUBLIC AUCTION", "not_a_sale_day"),       # a Thursday
    ("WILL ON 2/5/2207 SELL AT PUBLIC AUCTION", "date_out_of_reach"),    # a typo'd year
    ("WILL ON 2/30/2027 SELL", "impossible_date"),
    ("SALE DATE TBD; INSPECTION ON 2/5/2027", "no_sale_construction"),   # Codex's example
    ("SALE DATE TBD; INSPECTION WILL ON 2/5/2027 BEGIN AT 9:00 AM", "no_sale_construction"),
    ("SALE CONFIRMED. RECORDED 2/5/2027 AT THE ANNEX", "no_sale_construction"),
])
def test_a_date_that_could_be_anything_else_is_held(remarks, why):
    assert d.parse_sale(remarks, date(2026, 10, 1))[2] == why


@pytest.mark.parametrize("day,ok", [
    (date(2027, 2, 5), True),      # an ordinary Friday
    (date(2026, 12, 25), False),   # Christmas on a Friday is no sale day...
    (date(2026, 12, 28), True),    # ...the sale moves to the Monday
    (date(2027, 1, 11), False),    # an ordinary Monday
    (date(2027, 1, 1), False),     # New Year's Day, a Friday
    (date(2027, 1, 4), True),      # the Monday after it
    (date(2026, 11, 27), False),   # the day after Thanksgiving (a Friday holiday)
    (date(2026, 11, 30), True),
    (date(2027, 6, 18), False),    # Juneteenth 2027 is a Saturday: observed Friday 6/18
    (date(2027, 6, 21), True),     # so the Monday after is a sale day
    (date(2027, 9, 6), False),     # Labor Day is a Monday holiday, never a sale day
])
def test_the_lawful_sale_day_calendar(day, ok):
    assert d.is_sale_day(day) is ok


@pytest.mark.parametrize("change,why", [
    ({"apns": ["96600001800", "96600001900"]}, "several_parcels"),
    ({"apns": []}, "no_parcel"),
    ({"apns": ["123456789012"]}, "parcel_shape"),
    ({"doc_code": "ANTS"}, "not_an_original_notice"),    # amendments are held for a person
    ({"doc_code": "NDTS"}, "not_an_original_notice"),
])
def test_a_notice_that_cannot_be_pinned_to_one_property_is_held(change, why):
    p = {**d.parse_detail(_HTML), **change}
    assert d.notice_to_row(p, "u", date(2026, 11, 17)) == (None, why)
