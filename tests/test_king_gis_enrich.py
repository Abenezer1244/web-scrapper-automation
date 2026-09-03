"""King County GIS address enrichment — parse-layer contract (no network).

Covers the Codex-reviewed design: full "STREET, CITY, STATE ZIP" property address,
NO property->mailing echo for King (taxpayer mailing isn't public), and vacant/
raw-land parcels returned matched (so they don't fall through to the statewide
service) with property_address NULL + situs location for display only.
"""
from src.scrapers.enrichment.county_gis import _KNOWN_GIS_ENDPOINTS, _parse_gis_response

KING = _KNOWN_GIS_ENDPOINTS["king_WA"]
PIERCE = _KNOWN_GIS_ENDPOINTS["pierce_WA"]


def _feat(attrs: dict) -> dict:
    return {"features": [{"attributes": attrs}]}


def test_king_config_shape():
    assert KING["parcel_field"] == "PIN"
    assert KING["address_field"] == "ADDR_FULL"
    assert KING["mailing_fields"] == []
    assert KING["echo_property_to_mailing"] is False
    assert KING["skip_statewide_fallback"] is True
    assert KING["address_suffix_fields"] == ["POSTALCTYNAME", "STATE_ABBR", "ZIP5"]


def test_king_addressed_parcel_builds_full_address_and_no_mailing_echo():
    out = _parse_gis_response(
        _feat({"PIN": "0357000080", "ADDR_FULL": "1136 31ST AVE S",
               "POSTALCTYNAME": "SEATTLE", "STATE_ABBR": "WA", "ZIP5": "98144"}),
        KING,
    )
    assert out["property_address"] == "1136 31ST AVE S, SEATTLE, WA 98144"
    assert out["mailing_address"] is None       # King never echoes property->mailing
    assert out["matched"] is True
    assert out["vacant_no_situs"] is False


def test_king_vacant_parcel_keeps_property_null_but_matched_with_situs():
    out = _parse_gis_response(
        _feat({"PIN": "0007400034", "ADDR_FULL": None,
               "POSTALCTYNAME": None, "STATE_ABBR": None, "ZIP5": "98118"}),
        KING,
    )
    assert out["property_address"] is None      # never fabricate a street for raw land
    assert out["mailing_address"] is None
    assert out["matched"] is True               # matched -> won't fall through to statewide
    assert out["vacant_no_situs"] is True
    assert out["situs_zip"] == "98118"


def test_king_no_feature_is_unmatched():
    out = _parse_gis_response({"features": []}, KING)
    assert out["matched"] is False
    assert out["property_address"] is None


def test_pierce_unchanged_street_only_and_real_mailing():
    # Pierce has no address_suffix_fields (property stays street-only) and real
    # mailing_fields — the King changes must not alter it.
    out = _parse_gis_response(
        _feat({"TaxParcelNumber": "1234", "Site_Address": "123 MAIN ST",
               "Delivery_Address": "PO BOX 9", "City_State": "TACOMA WA", "Zipcode": "98402"}),
        PIERCE,
    )
    assert out["property_address"] == "123 MAIN ST"
    assert out["mailing_address"] == "PO BOX 9, TACOMA WA, 98402"
    assert out["matched"] is True
