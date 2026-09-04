"""Guard rails of scripts/repair_probate_party_and_bad_parcel.py.

Follows the convention of the other backfill tests: load the script and assert its
pure decision logic and the SHAPE of every statement it can execute, so a future
edit cannot quietly widen what the repair writes.
"""
import importlib.util
from pathlib import Path

_SCRIPT = (
    Path(__file__).parent.parent / "scripts" / "repair_probate_party_and_bad_parcel.py"
)
_spec = importlib.util.spec_from_file_location("repair_probate_party_and_bad_parcel", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_party_update_writes_only_the_identity_columns():
    sql = " ".join(str(_mod._PARTY_UPDATE).split())
    assert "SET party_name = :new_party, heirs = :new_heirs" in sql
    # The repair must never touch the FROZEN identity/billing keys or the parcel.
    for frozen in ("parcel_id", "dedup_hash", "property_key", "source_fingerprint",
                   "is_duplicate", "record_count"):
        assert frozen not in sql


def test_party_update_is_guarded_on_the_values_it_read():
    sql = " ".join(str(_mod._PARTY_UPDATE).split())
    assert "party_name IS NOT DISTINCT FROM :old_party" in sql
    assert "heirs IS NOT DISTINCT FROM :old_heirs" in sql


def test_parcel_update_clears_the_wrong_attribution_and_nothing_else():
    sql = " ".join(str(_mod._PARCEL_UPDATE).split())
    for cleared in ("property_address = NULL", "property_city = NULL",
                    "property_state = NULL", "property_zip = NULL"):
        assert cleared in sql
    # parcel_id is preserved exactly as the county printed it — it feeds the frozen
    # dedup_hash, and no 10-digit candidate can be derived without guessing.
    assert "SET parcel_id" not in sql
    assert "parcel_id = :parcel_id" in sql          # guard, not assignment
    for frozen in ("dedup_hash", "property_key", "source_fingerprint", "party_name"):
        assert frozen not in sql


def test_parcel_update_is_guarded_on_the_row_it_read():
    sql = " ".join(str(_mod._PARCEL_UPDATE).split())
    assert "WHERE id = :id" in sql
    assert "property_address IS NOT DISTINCT FROM :old_property" in sql


def test_assessor_derived_keys_are_the_ones_read_off_the_wrong_page():
    # Both were computed FROM the mismatched parcel's page, so both must go.
    assert set(_mod._ASSESSOR_DERIVED_KEYS) == {"assessor_current_owner", "title_status"}


def test_cancelling_a_trace_only_touches_a_still_queued_row():
    cancel = " ".join(str(_mod._CANCEL_PENDING).split())
    assert "SET status = 'errored'" in cancel
    assert "AND status = 'queued'" in cancel      # never a submitted/completed trace
    reset = " ".join(str(_mod._RESET_RESULT_TRACE).split())
    assert "SET skip_trace_status = 'not_attempted'" in reset
    assert "AND skip_trace_status = 'queued'" in reset


def test_default_clearing_scope_is_the_audited_record_types():
    assert _mod._DEFAULT_RECORD_TYPES == ("probate", "death_certificate")


def test_party_candidates_are_scoped_to_probate():
    sql = " ".join(str(_mod._PARTY_CANDIDATES).split())
    assert "sc.record_type IN ('probate', 'death_certificate')" in sql


def test_parcel_candidates_are_king_rows_with_a_malformed_pin():
    sql = " ".join(str(_mod._PARCEL_CANDIDATES).split())
    assert "lower(sc.county) = 'king'" in sql
    assert "length(btrim(r.parcel_id)) <> :pin_len" in sql
    assert _mod._KING_PIN_DIGITS == 10
