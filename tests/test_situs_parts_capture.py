"""Migration 085 fill paths (Phase 4): structured situs parts come only from real
sources — the scraper's full line, a statewide SITUS row, or a county row whose
Delivery_Address equals the Site_Address. Real Pierce ArcGIS features (2026-09-02)."""
from types import SimpleNamespace

from src.scrapers.enrichment.county_gis import _KNOWN_GIS_ENDPOINTS, _parse_gis_response
from src.workers.tasks_helpers.enrich import _TRAILING_ZIP_RE, _keep_situs_parts

_CFG = _KNOWN_GIS_ENDPOINTS["pierce_WA"]
_OWNER_OCCUPIED = {"attributes": {
    "TaxParcelNumber": "6025430870", "Site_Address": "9226 175TH STREET CT E",
    "Delivery_Address": "9226 175TH STREET CT E", "City_State": "PUYALLUP, WA", "Zipcode": "98375-4018"}}
_ABSENTEE = {"attributes": {
    "TaxParcelNumber": "0317153007", "Site_Address": "1311 334TH ST E",
    "Delivery_Address": "PO BOX 4416", "City_State": "SPANAWAY, WA", "Zipcode": "98387-4027"}}


def _res(**kw):
    base = {"property_address": None, "mailing_address": None, "property_city": None,
            "property_state": None, "property_zip": None}
    base.update(kw)
    return SimpleNamespace(**base)


def test_pierce_delivery_equals_site_yields_situs_parts():
    p = _parse_gis_response({"features": [_OWNER_OCCUPIED]}, _CFG)
    assert (p["property_city"], p["property_state"], p["property_zip"]) == ("PUYALLUP", "WA", "98375-4018")


def test_pierce_po_box_owner_yields_no_situs_parts():
    p = _parse_gis_response({"features": [_ABSENTEE]}, _CFG)
    assert "property_city" not in p  # the mailing city is NOT the property's


def test_scraper_full_line_is_kept_before_gis_overwrite():
    res = _res(property_address="22109 43RD AVENUE EAST, SPANAWAY, WA 98387")
    _keep_situs_parts(res, {"property_address": "22109 43RD AVE E", "mailing_address": None})
    assert (res.property_city, res.property_state, res.property_zip) == ("SPANAWAY", "WA", "98387")


def test_gis_parts_fill_only_empty_slots():
    res = _res(property_address="9226 175TH ST CT E", property_city="PUYALLUP")
    _keep_situs_parts(res, {"property_address": "9226 175TH STREET CT E",
                            "property_city": "OTHER", "property_state": "WA", "property_zip": "98375"})
    assert (res.property_city, res.property_state, res.property_zip) == ("PUYALLUP", "WA", "98375")


def test_street_only_line_and_bare_gis_row_fill_nothing():
    res = _res(property_address="9226 175TH ST CT E")
    _keep_situs_parts(res, {"property_address": "9226 175TH STREET CT E"})
    assert (res.property_city, res.property_state, res.property_zip) == (None, None, None)


def test_trailing_zip_regex_is_anchored():
    assert _TRAILING_ZIP_RE.search("2019 SW 318TH PL 4C 98023").group(1) == "98023"
    assert _TRAILING_ZIP_RE.search("8822 2ND AVE S 98108-4501").group(1) == "98108"
    assert _TRAILING_ZIP_RE.search("12345 MAIN ST") is None
