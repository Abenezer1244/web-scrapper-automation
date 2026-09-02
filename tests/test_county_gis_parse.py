"""GIS response shaping (src/scrapers/enrichment/county_gis.py) — pure, no network.

Pins the 2026-09-02 fix: a parcel layer that has NO owner/mailing fields (the WA
statewide Current_Parcels fallback, or a generic config without mailing_fields)
must NOT copy the situs into `mailing_address`. That copy read downstream as a
county-supplied mailing address: absentee_owner=False ("owner-occupied") and a
Mailing column the county never provided. The situs locality is kept on
property_address so skip trace still gets city/state/zip.
"""
from src.scrapers.enrichment.county_gis import (
    _KNOWN_GIS_ENDPOINTS,
    _make_generic_config,
    _parse_gis_response,
    _statewide_result,
)


class TestStatewideResult:
    def test_mailing_is_never_fabricated(self):
        r = _statewide_result("123 MAIN ST", "OLYMPIA", "98501", "12345678")
        assert r["mailing_address"] is None
        assert r["parcel_id"] == "12345678"

    def test_locality_kept_on_property_address(self):
        r = _statewide_result("123 MAIN ST", "OLYMPIA", "98501", "1")
        assert r["property_address"] == "123 MAIN ST, OLYMPIA, WA 98501"

    def test_city_without_zip(self):
        r = _statewide_result("123 MAIN ST", "OLYMPIA", "", "1")
        assert r["property_address"] == "123 MAIN ST, OLYMPIA, WA"

    def test_no_city_keeps_bare_street(self):
        # "STREET, WA 98501" would be mis-parsed as city="WA 98501" downstream
        r = _statewide_result("123 MAIN ST", "", "98501", "1")
        assert r["property_address"] == "123 MAIN ST"
        assert r["mailing_address"] is None


class TestParseGisResponse:
    def _feature(self, **attrs):
        return {"features": [{"attributes": attrs}]}

    def test_pierce_layer_builds_mailing_from_its_own_fields(self):
        cfg = _KNOWN_GIS_ENDPOINTS["pierce_WA"]
        r = _parse_gis_response(self._feature(
            TaxParcelNumber="8996011270",
            Site_Address="20508 ISLAND PKWY",
            Delivery_Address="20508 ISLAND PKWY E",
            City_State="LAKE TAPPS, WA",
            Zipcode="98391-9081",
        ), cfg)
        assert r["property_address"] == "20508 ISLAND PKWY"
        assert r["mailing_address"] == "20508 ISLAND PKWY E, LAKE TAPPS, WA, 98391-9081"

    def test_pierce_layer_without_delivery_fields_leaves_mailing_none(self):
        cfg = _KNOWN_GIS_ENDPOINTS["pierce_WA"]
        r = _parse_gis_response(self._feature(
            TaxParcelNumber="1", Site_Address="1 A ST", Delivery_Address=None,
            City_State=None, Zipcode=None,
        ), cfg)
        assert r["property_address"] == "1 A ST"
        assert r["mailing_address"] is None

    def test_config_without_mailing_fields_does_not_copy_situs(self):
        cfg = {"address_field": "Site_Address", "parcel_field": "TaxParcelNumber"}
        r = _parse_gis_response(self._feature(TaxParcelNumber="9", Site_Address="1 A ST"), cfg)
        assert r["property_address"] == "1 A ST"
        assert r["mailing_address"] is None

    def test_generic_config_declares_mailing_fields(self):
        # The generic ArcGIS config reads real Delivery_Address fields, so a
        # layer that carries them still yields a genuine mailing address.
        cfg = _make_generic_config("https://example.invalid/arcgis/rest/services/x/FeatureServer/0/query")
        r = _parse_gis_response(self._feature(
            TaxParcelNumber="9", Site_Address="1 A ST",
            Delivery_Address="PO BOX 5", City_State="TACOMA, WA", Zipcode="98401",
        ), cfg)
        assert r["mailing_address"] == "PO BOX 5, TACOMA, WA, 98401"

    def test_empty_features(self):
        assert _parse_gis_response({"features": []}, _KNOWN_GIS_ENDPOINTS["pierce_WA"]) == {
            "property_address": None, "mailing_address": None,
        }
