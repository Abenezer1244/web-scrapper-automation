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
    _parse_gis_response,
    _situs_parts,
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


class TestStatewideSitusParts:
    """The statewide layer is situs-only: it yields the PROPERTY's city/state/zip and
    never a mailing address (2026-09-02 policy: no assumed owner-occupancy)."""

    def test_city_and_zip(self):
        assert _situs_parts("PUYALLUP", "98375") == {
            "property_city": "PUYALLUP", "property_state": "WA", "property_zip": "98375",
        }

    def test_missing_parts_are_none_not_guessed(self):
        # The live statewide row for APN 6025430870 has SITUS_CITY_NM/ZIP = null.
        assert _situs_parts("", "") == {
            "property_city": None, "property_state": "WA", "property_zip": None,
        }

    def test_whitespace_normalised(self):
        assert _situs_parts("  UNIVERSITY   PLACE ", " 98467 ")["property_city"] == "UNIVERSITY PLACE"


class TestNoSitusAsMailing:
    def test_generic_gis_config_without_mailing_fields_leaves_mailing_unknown(self):
        cfg = {"parcel_field": "APN", "address_field": "SITUS"}
        parsed = _parse_gis_response(
            {"features": [{"attributes": {"APN": "1", "SITUS": "1 MAIN ST"}}]}, cfg
        )
        assert parsed["property_address"] == "1 MAIN ST"
        assert parsed["mailing_address"] is None

    def test_pierce_config_still_builds_real_mailing(self):
        parsed = _parse_gis_response({"features": [_PIERCE_FEATURE]}, _PIERCE_CFG)
        assert parsed["mailing_address"] == "9226 175TH STREET CT E, PUYALLUP, WA, 98375-4018"
