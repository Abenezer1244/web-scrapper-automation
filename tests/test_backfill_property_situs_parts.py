"""Pure logic of scripts/backfill_property_situs_parts.py (Phase 5)."""
import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parent.parent / "scripts" / "backfill_property_situs_parts.py"
_spec = importlib.util.spec_from_file_location("backfill_property_situs_parts", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_parts_from_full_notice_line():
    assert _mod.parts_from_line("22109 43RD AVENUE EAST, SPANAWAY, WA 98387") == {
        "property_city": "SPANAWAY", "property_state": "WA", "property_zip": "98387"}


def test_parts_from_street_only_line_are_empty():
    assert _mod.parts_from_line("22109 43RD AVE E") == {}
    assert _mod.parts_from_line(None) == {}


def test_merge_fills_empty_slots_in_evidence_order():
    current = {"property_city": None, "property_state": None, "property_zip": "98387"}
    parts, prov = _mod.merge_parts(
        current,
        ("notice", {"property_city": "SPANAWAY", "property_state": "WA", "property_zip": "99999"}),
        ("gis", {"property_city": "OTHER"}),
    )
    assert parts == {"property_city": "SPANAWAY", "property_state": "WA", "property_zip": "98387"}
    assert prov == ["property_city<-notice", "property_state<-notice"]


def test_update_is_fill_only():
    sql = str(_mod._UPDATE)
    assert "COALESCE(property_city, :city)" in sql
    assert "WHERE id = :id AND (property_city IS NULL OR property_zip IS NULL)" in sql
