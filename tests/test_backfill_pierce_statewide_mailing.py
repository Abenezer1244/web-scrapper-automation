"""Decision logic of scripts/backfill_pierce_statewide_mailing.py (pure parts only;
the DB/GIS run is exercised as a prod dry-run). Uses the real Pierce ArcGIS feature
shapes from 2026-09-02."""
import importlib.util
from pathlib import Path

from src.scrapers.enrichment.county_gis import _KNOWN_GIS_ENDPOINTS, _map_county_features

_SCRIPT = Path(__file__).parent.parent / "scripts" / "backfill_pierce_statewide_mailing.py"
_spec = importlib.util.spec_from_file_location("backfill_pierce_statewide_mailing", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_CFG = _KNOWN_GIS_ENDPOINTS["pierce_WA"]
# Real rows: a PO-BOX owner and an owner-occupied one.
_PO_BOX = {"attributes": {"TaxParcelNumber": "0317153007", "Site_Address": "1311 334TH ST E",
                          "Delivery_Address": "PO BOX 4416", "City_State": "SPANAWAY, WA",
                          "Zipcode": "98387-4027"}}


def test_county_row_wins_and_flags_recomputed():
    county = _map_county_features([_PO_BOX], _CFG, {"0317153007": ["031715-3007"]})["031715-3007"]
    mailing, action = _mod.plan_row("1311 334TH ST E", county)
    assert mailing == "PO BOX 4416, SPANAWAY, WA, 98387-4027"
    assert action == "county_mailing"
    flags = _mod.compute_owner_flags("1311 334TH ST E", mailing)
    assert flags["absentee_owner"] is True  # the fabricated line had said owner-occupied


def test_no_county_row_means_null_not_situs():
    mailing, action = _mod.plan_row("4615 BROOKDALE RD E", None)
    assert mailing is None
    assert action == "null_no_county_row"
    assert _mod.compute_owner_flags("4615 BROOKDALE RD E", None)["absentee_owner"] is None


def test_candidate_sql_pins_exact_fabricated_signature():
    sql = str(_mod._CANDIDATES)
    assert "r.mailing_address = r.property_address || ', WA'" in sql
    assert "sc.county = 'pierce'" in sql


def test_update_is_guarded_on_old_value():
    assert "WHERE id = :id AND mailing_address = :old" in str(_mod._UPDATE)
