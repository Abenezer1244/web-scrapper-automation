"""Pierce ATIP address fallback — pure parsing/classification (no network, no captcha).

The live behaviour these lock in was verified on 2026-09-02 against
atip.piercecountywa.gov with a real 2Captcha Enterprise token: missing/rejected
token -> HTTP 200 + EMPTY body; unknown parcel -> "[]"; known -> JSON list.
"""
from src.scrapers.enrichment.pierce_atip import (
    FOUND,
    HARD_FAILURE,
    NOT_FOUND,
    TOKEN_REJECTED,
    classify_response,
    parse_summary,
)

_LIVE_ROW = {
    "parcel_number": "5000050810", "acct_type": "Mobile Home",
    "situs": "7612 159TH ST E #151", "mail": "7612 159TH ST E SPC 151",
    "mail2": None, "mail3": None, "city": "PUYALLUP", "zip": "98375-7130",
    "care_of": None, "state": "WA", "country": None,
    "use_cd": "1152-MOBILE/MFG HOME", "name": "CALVO JOVANNY M & DELGADO LIZETTE",
    "tax_year": "2027", "category": "Mobile Home", "fclr_status": "DSRT",
}


def test_classify_empty_body_is_token_rejection_only_when_truly_empty():
    assert classify_response(200, "")[0] == TOKEN_REJECTED
    assert classify_response(200, "  \n")[0] == TOKEN_REJECTED
    assert classify_response(200, "[]")[0] == NOT_FOUND          # unknown parcel: never retried
    assert classify_response(200, "[ ]")[0] == NOT_FOUND


def test_classify_found_and_failures():
    kind, rows = classify_response(200, '[{"situs": "1 MAIN ST"}]')
    assert kind == FOUND and rows == [{"situs": "1 MAIN ST"}]
    assert classify_response(500, "")[0] == HARD_FAILURE
    assert classify_response(403, "[]")[0] == HARD_FAILURE
    assert classify_response(200, "<html>blocked</html>")[0] == HARD_FAILURE
    assert classify_response(200, '{"error": 1}')[0] == HARD_FAILURE


def test_parse_summary_takes_addresses_but_never_the_name():
    out = parse_summary(_LIVE_ROW)
    assert out == {
        "property_address": "7612 159TH ST E #151",
        "mailing_address": "7612 159TH ST E SPC 151, PUYALLUP, WA, 98375-7130",
        "atip_account_type": "Mobile Home",
        "atip_use_code": "1152-MOBILE/MFG HOME",
    }
    assert "name" not in out and "CALVO" not in str(out)


def test_parse_summary_mailing_shape_matches_gis_and_handles_gaps():
    row = dict(_LIVE_ROW, mail="PO BOX 1", mail2="C/O SOMEONE", mail3="  ", city="TACOMA", zip=None)
    assert parse_summary(row)["mailing_address"] == "PO BOX 1, C/O SOMEONE, TACOMA, WA"
    assert parse_summary(dict(_LIVE_ROW, mail=None, mail2=None))["mailing_address"] is None
    assert parse_summary(dict(_LIVE_ROW, situs=None)) is None
    assert parse_summary(dict(_LIVE_ROW, situs="   ")) is None
