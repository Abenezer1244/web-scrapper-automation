"""County-GIS batch enrichment: results must be keyed by the CALLER's parcel id.

Uses a REAL Pierce County Tax_Parcels ArcGIS feature (queried 2026-09-02 for APN
6025430870, the "Test 3" lead whose trustee printed the parcel as 602543-087-0).
The worker applies GIS rows by the lead's raw parcel_id, so a dict keyed by the
server's canonical spelling silently dropped every dashed parcel (11 of 33 live
Pierce NTS notices) — those leads fell through to the situs-only statewide service
and shipped a fabricated "STREET, WA" mailing address.
"""
from src.scrapers.enrichment.county_gis import (
    _KNOWN_GIS_ENDPOINTS,
    _arcgis_literal,
    _map_county_features,
    _statewide_mailing,
)


class TestArcgisLiteral:
    def test_plain(self):
        assert _arcgis_literal("6025430870") == "'6025430870'"

    def test_quote_is_doubled_not_injected(self):
        assert _arcgis_literal("60254' OR 1=1 --") == "'60254'' OR 1=1 --'"

_PIERCE_CFG = _KNOWN_GIS_ENDPOINTS["pierce_WA"]

# Verbatim attributes returned by the Pierce endpoint for TaxParcelNumber='6025430870'.
_PIERCE_FEATURE = {
    "attributes": {
        "TaxParcelNumber": "6025430870",
        "Site_Address": "9226 175TH STREET CT E",
        "Delivery_Address": "9226 175TH STREET CT E",
        "City_State": "PUYALLUP, WA",
        "Zipcode": "98375-4018",
        "Business_Name": "NAVARRO PDD",
        "Legal_Description": (
            "Section 28  Township 19  Range 04  Quarter 34   Plat   NAVARRO PDD  LOT 87 "
            "TOG/W UND INT IN TRS B, C, D & E EASE OF RECORD OUT OF 602238-004-1 SEG "
            "2007-0414 JU 11/16/06JU"
        ),
    }
}


class TestMapCountyFeatures:
    def test_dashed_caller_id_receives_the_county_row(self):
        res = _map_county_features([_PIERCE_FEATURE], _PIERCE_CFG, {"6025430870": ["602543-087-0"]})
        assert list(res) == ["602543-087-0"]
        row = res["602543-087-0"]
        assert row["property_address"] == "9226 175TH STREET CT E"
        assert row["mailing_address"] == "9226 175TH STREET CT E, PUYALLUP, WA, 98375-4018"
        assert row["parcel_id"] == "6025430870"  # canonical form from the server

    def test_plain_caller_id_keyed_as_is(self):
        res = _map_county_features([_PIERCE_FEATURE], _PIERCE_CFG, {"6025430870": ["6025430870"]})
        assert list(res) == ["6025430870"]

    def test_two_spellings_of_one_apn_both_enriched(self):
        # Two leads in one job can carry the same APN as "602543-087-0" and
        # "6025430870"; both must receive the county row (Codex).
        res = _map_county_features(
            [_PIERCE_FEATURE], _PIERCE_CFG, {"6025430870": ["602543-087-0", "6025430870"]}
        )
        assert set(res) == {"602543-087-0", "6025430870"}
        assert res["602543-087-0"] is not res["6025430870"]  # independent copies
        assert res["602543-087-0"] == res["6025430870"]

    def test_unknown_server_id_falls_back_to_server_key(self):
        res = _map_county_features([_PIERCE_FEATURE], _PIERCE_CFG, {})
        assert list(res) == ["6025430870"]

    def test_feature_without_address_is_skipped(self):
        feature = {"attributes": {**_PIERCE_FEATURE["attributes"], "Site_Address": None}}
        assert _map_county_features([feature], _PIERCE_CFG, {"6025430870": ["602543-087-0"]}) == {}


class TestStatewideMailing:
    _ADDR = "9226 175TH STREET CT E"

    def test_no_city_no_zip_emits_nothing(self):
        # The live statewide row for APN 6025430870 has SITUS_CITY_NM/ZIP = null; the
        # old code fabricated "9226 175TH STREET CT E, WA".
        assert _statewide_mailing(self._ADDR, "", "") is None

    def test_city_only(self):
        assert _statewide_mailing(self._ADDR, "PUYALLUP", "") == "9226 175TH STREET CT E, PUYALLUP, WA"

    def test_zip_only(self):
        assert _statewide_mailing(self._ADDR, "", "98375") == "9226 175TH STREET CT E, WA 98375"

    def test_city_and_zip(self):
        assert (
            _statewide_mailing(self._ADDR, "PUYALLUP", "98375")
            == "9226 175TH STREET CT E, PUYALLUP, WA 98375"
        )
