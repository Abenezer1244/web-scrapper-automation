"""GIS response shaping (src/scrapers/enrichment/county_gis.py) — pure, no network.

Pins the 2026-09-02 policy: a parcel layer that has NO owner/mailing fields (the WA
statewide Current_Parcels fallback, or a generic config without mailing_fields) must
NOT copy the situs into `mailing_address`. That copy read downstream as a
county-supplied mailing address: absentee_owner=False ("owner-occupied") and a
Mailing column the county never provided.

The situs locality it DOES know travels in the structured `property_city` /
`property_state` / `property_zip` parts (migration 085) beside a FROZEN street-only
`property_address` — not folded into the address string, which is an identity/cache
key elsewhere.
"""
from src.scrapers.enrichment.county_gis import (
    _KNOWN_GIS_ENDPOINTS,
    _LIKE_META,
    _arcgis_literal,
    _make_generic_config,
    _parse_gis_response,
    _situs_parts,
    _situs_parts_from_confirmed_mailing,
)


class TestSitusParts:
    """WA statewide rows: structured situs parts, never a fabricated mailing line."""

    def test_city_and_zip_are_kept_as_parts(self):
        assert _situs_parts("OLYMPIA", "98501") == {
            "property_city": "OLYMPIA", "property_state": "WA", "property_zip": "98501",
        }

    def test_state_is_wa_by_construction(self):
        # The layer IS the WA parcel service, queried with the county FIPS.
        assert _situs_parts("", "")["property_state"] == "WA"

    def test_missing_city_or_zip_is_none_not_empty_string(self):
        r = _situs_parts("", "98501")
        assert r["property_city"] is None and r["property_zip"] == "98501"
        r = _situs_parts("OLYMPIA", "")
        assert r["property_city"] == "OLYMPIA" and r["property_zip"] is None

    def test_internal_whitespace_is_collapsed(self):
        assert _situs_parts("  GIG   HARBOR ", " 98335 ")["property_city"] == "GIG HARBOR"


class TestSitusPartsFromConfirmedMailing:
    """County rows only yield situs parts when the county itself says the owner's
    mail goes to the property (Delivery_Address == Site_Address, not a PO box)."""

    def _cfg(self):
        return {"mailing_fields": ["Delivery_Address", "City_State", "Zipcode"]}

    def test_confirmed_owner_occupied_yields_parts(self):
        r = _situs_parts_from_confirmed_mailing(
            {"Delivery_Address": "20508 ISLAND PKWY E", "City_State": "LAKE TAPPS, WA",
             "Zipcode": "98391"},
            "20508 ISLAND PKWY E", self._cfg(),
        )
        assert r == {"property_city": "LAKE TAPPS", "property_state": "WA",
                     "property_zip": "98391"}

    def test_mailing_elsewhere_yields_nothing(self):
        assert _situs_parts_from_confirmed_mailing(
            {"Delivery_Address": "1 OTHER ST", "City_State": "TACOMA, WA", "Zipcode": "98401"},
            "20508 ISLAND PKWY E", self._cfg(),
        ) == {}

    def test_po_box_yields_nothing(self):
        assert _situs_parts_from_confirmed_mailing(
            {"Delivery_Address": "PO BOX 5", "City_State": "TACOMA, WA", "Zipcode": "98401"},
            "PO BOX 5", self._cfg(),
        ) == {}


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


class TestArcgisPredicateQuoting:
    """A raw-interpolated ArcGIS `where` value breaks the predicate the moment a
    real value contains a quote. Parcel predicates were fixed in #184/#186; the
    owner-name LIKE was missed (2026-09-03)."""

    def test_apostrophe_surname_is_escaped_not_broken(self):
        # O'BRIEN, O'CONNOR, D'ANGELO are ordinary WA surnames. Raw
        # interpolation produced LIKE 'O'BRIEN%' — malformed; ArcGIS errored and
        # the caller's except swallowed it, so those owners silently got no
        # enrichment at all.
        assert _arcgis_literal("O'BRIEN%") == "'O''BRIEN%'"

    def test_wildcard_stays_a_wildcard_after_escaping(self):
        # The % must sit INSIDE the literal, still acting as the LIKE wildcard.
        lit = _arcgis_literal("SMITH" + "%")
        assert lit.startswith("'") and lit.endswith("%'")

    def test_plain_surname_is_unchanged_apart_from_quoting(self):
        assert _arcgis_literal("SMITH%") == "'SMITH%'"

    def test_like_metacharacters_are_detected(self):
        # Rejected, not rewritten — same rule as pierce_legal_repair._LIKE_META.
        assert _LIKE_META.search("SMITH%") is not None
        assert _LIKE_META.search("SMITH_JR") is not None
        assert _LIKE_META.search("OBRIEN") is None


class TestFallbackOrder:
    """An EXACT parcel lookup must always beat a fuzzy owner-name lookup.

    The name search reduces the owner to its first token and asks ArcGIS for ONE
    row, so for a common surname it returns *some* parcel owned by *someone*
    similarly named. Before 2026-09-03 it ran BEFORE the exact WA statewide
    parcel lookup, so a name hit could pre-empt the real APN match and attach
    the wrong address to a lead. Escaping the predicate (so O'BRIEN stopped
    erroring out) is exactly what would have made that misfire reachable.
    """

    def _patch(self, monkeypatch, calls):
        from src.scrapers.enrichment import county_gis as cg

        def rec(name, ret):
            def _f(*a, **kw):
                calls.append(name)
                return ret
            return _f

        monkeypatch.setattr(cg.settings, "GIS_ENRICHMENT_ENABLED", True, raising=False)
        # county parcel lookup misses (this is the trigger for any fallback)
        monkeypatch.setattr(cg, "_query_gis", rec("county_parcel", cg._empty()))
        monkeypatch.setattr(cg, "_query_gis_by_name",
                            rec("name", {"property_address": "999 WRONG ST",
                                         "mailing_address": None}))
        monkeypatch.setattr(cg, "_query_wa_statewide",
                            rec("statewide_parcel", {"property_address": "1 RIGHT ST",
                                                     "mailing_address": None}))
        monkeypatch.setattr(cg, "_query_wa_statewide_by_name",
                            rec("statewide_name", cg._empty()))
        return cg

    def test_exact_parcel_wins_over_owner_name(self, monkeypatch):
        calls = []
        cg = self._patch(monkeypatch, calls)
        out = cg.enrich_parcel_gis(
            parcel_id="0121228036", county="pierce", state="WA", owner_name="O'BRIEN JOHN",
        )
        assert out["property_address"] == "1 RIGHT ST"
        # The name search must not even be consulted while a parcel id exists.
        assert "name" not in calls
        assert calls == ["county_parcel", "statewide_parcel"]

    def test_name_search_still_runs_when_there_is_no_parcel(self, monkeypatch):
        calls = []
        cg = self._patch(monkeypatch, calls)
        out = cg.enrich_parcel_gis(
            parcel_id=None, county="pierce", state="WA", owner_name="O'BRIEN JOHN",
        )
        # No parcel to match on, so a name search is the only option left.
        assert out["property_address"] == "999 WRONG ST"
        assert "name" in calls
