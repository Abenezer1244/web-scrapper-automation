"""Rule logic of scripts/backfill_assumed_mailing.py (pure decide()); the DB/GIS run
is exercised as a prod dry-run with an evidence file."""
import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parent.parent / "scripts" / "backfill_assumed_mailing.py"
_spec = importlib.util.spec_from_file_location("backfill_assumed_mailing", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
decide = _mod.decide
OLD = "712 143RD PL SW, LYNNWOOD, WA 98087-6429"


def test_snohomish_has_no_source_so_unknown():
    assert decide("S", OLD, None) == ("write", None, "none_no_source")


def test_king_found_is_written_even_when_it_equals_the_situs():
    lk = {"mailing_lookup": "found", "mailing_address": OLD}
    assert decide("K", OLD, lk) == ("write", OLD, "king_assessor_tax_bill")


def test_king_none_means_source_says_no_mailing():
    assert decide("K", OLD, {"mailing_lookup": "none", "mailing_address": None}) == (
        "write", None, "none_king_assessor_no_mailing_block")


def test_king_error_or_missing_leaves_row_alone():
    assert decide("K", OLD, {"mailing_lookup": "error", "mailing_address": None})[0] == "skip"
    assert decide("K", OLD, {"mailing_lookup": "not_attempted", "mailing_address": None})[0] == "skip"
    assert decide("K", OLD, None) == ("skip", None, "king_lookup_missing")


def test_pierce_confirm_vs_refresh_vs_unverified():
    same = {"mailing_address": "712 143RD PL SW,  LYNNWOOD, WA  98087-6429"}
    assert decide("P", OLD, same) == ("confirm", OLD, "pierce_county_gis")
    # county dropped the ZIP+4 → same place; keep the richer stored value
    zip5 = {"mailing_address": "712 143RD PL SW, LYNNWOOD, WA, 98087"}
    assert decide("P", OLD, zip5) == ("confirm", OLD, "pierce_county_gis")
    diff = {"mailing_address": "PO BOX 4416, SPANAWAY, WA, 98387-4027"}
    assert decide("P", OLD, diff) == ("write", diff["mailing_address"], "pierce_county_gis")
    assert decide("P", OLD, None)[0] == "skip"


def test_sql_guards():
    assert "mailing_address = :old" in str(_mod._UPDATE)
    assert "mailing_source" in str(_mod._UPDATE)
    assert "LIKE upper(r.property_address) || '%'" in str(_mod._CANDIDATES)
