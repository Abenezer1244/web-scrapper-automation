"""License-restricted county GIS mailing stays off until counsel clears it.

The Snohomish parcel dataset's open-data terms say users "will not use any lists of
individuals, or data from which such lists may be compiled, for any commercial
purpose" (RCW 42.56.070(8)). Owner decision 2026-09-13: hold the Snohomish and
Cowlitz owner/taxpayer mailing source off, keep property-address enrichment.
Pure config tests; no network.
"""
import pytest

from src.config import settings
from src.scrapers.enrichment import county_gis as cg

_RESTRICTED = ("snohomish", "cowlitz", "douglas")


@pytest.mark.parametrize("county", _RESTRICTED)
def test_mailing_is_off_by_default_but_situs_lookup_remains(county):
    assert settings.COUNTY_GIS_RESTRICTED_MAILING_ENABLED is False
    cfg = cg._effective_gis_config(f"{county}_WA")
    assert cfg is not None and cfg["endpoint"]
    assert not any(k in cfg for k in cg._MAILING_KEYS)
    assert cfg["out_fields"] == cg._KNOWN_GIS_ENDPOINTS[f"{county}_WA"]["situs_only_out_fields"]
    assert cg.has_gis_mailing_source(county, "WA") is False


def test_gated_counties_request_no_owner_or_taxpayer_columns():
    for county in _RESTRICTED:
        fields = cg._effective_gis_config(f"{county}_WA")["out_fields"].lower()
        for owner_column in ("taxpr", "deed_holder", "address1", "address2"):
            assert owner_column not in fields, (county, owner_column)


def test_the_mailing_recovery_sweep_skips_gated_counties():
    listed = cg.gis_mailing_source_counties("WA")
    assert not set(_RESTRICTED) & set(listed)


@pytest.mark.parametrize("county", _RESTRICTED)
def test_enabling_the_setting_restores_the_full_config(county, monkeypatch):
    monkeypatch.setattr(settings, "COUNTY_GIS_RESTRICTED_MAILING_ENABLED", True)
    assert cg._effective_gis_config(f"{county}_WA") is cg._KNOWN_GIS_ENDPOINTS[f"{county}_WA"]
    assert cg.has_gis_mailing_source(county, "WA") is True


def test_unrestricted_counties_are_untouched():
    for key, cfg in cg._KNOWN_GIS_ENDPOINTS.items():
        if not cfg.get("mailing_license_restricted"):
            assert cg._effective_gis_config(key) is cfg



@pytest.mark.parametrize("gis_endpoint", [None, "https://gis.snoco.org/override/FeatureServer/0/query"])
def test_the_single_parcel_path_never_queries_with_mailing_fields(monkeypatch, gis_endpoint):
    # Codex P1: a connector-supplied gis_endpoint used to bypass the gate. Capture the
    # config handed to the county query instead of calling the network.
    seen: list[dict] = []

    def _capture(parcel_id, gis_config, county_key):
        seen.append(gis_config)
        return {"property_address": "1 MAIN ST", "mailing_address": None, "matched": True}

    monkeypatch.setattr(settings, "GIS_ENRICHMENT_ENABLED", True)
    monkeypatch.setattr(cg, "_query_gis", _capture)
    monkeypatch.setattr(cg, "_query_wa_statewide", lambda *a, **k: {"property_address": None, "mailing_address": None})
    cg.enrich_parcel_gis("00100000000001", "snohomish", "WA", gis_endpoint=gis_endpoint)
    assert len(seen) == 1
    assert not any(k in seen[0] for k in cg._MAILING_KEYS)
