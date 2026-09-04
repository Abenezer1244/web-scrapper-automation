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


def test_journal_records_every_column_the_parcel_update_nulls():
    # Codex P2: the evidence file must be enough to restore any row it cleared.
    src = _SCRIPT.read_text(encoding="utf-8")
    for key in ("cleared_property_address", "cleared_property_city",
                "cleared_property_state", "cleared_property_zip",
                "cleared_enrichment", "old_enrichment_data"):
        assert f'"{key}"' in src
    # ...and the party repair must journal both sides of what it rewrites.
    for key in ("old_party", "new_party", "old_heirs", "new_heirs"):
        assert f'"{key}"' in src


def test_a_trace_is_cancelled_only_after_the_clear_actually_wrote():
    # Codex P1: if the guarded clear no-ops because the row changed under us,
    # cancelling its queued trace would kill a lookup for an address this run did
    # not remove. The cancel must sit behind the rowcount check.
    src = _SCRIPT.read_text(encoding="utf-8")
    clear_at = src.index('stats["cleared"] += res.rowcount')
    guard_at = src.index("if res.rowcount:", clear_at)
    cancel_at = src.index("_CANCEL_PENDING", guard_at)
    reset_at = src.index("_RESET_RESULT_TRACE", guard_at)
    assert clear_at < guard_at < cancel_at < reset_at


def test_parcel_update_guards_every_value_it_overwrites():
    # Codex P2: the new enrichment_data is built from the copy we READ, so a
    # concurrent writer's JSON would be clobbered by a stale copy unless the JSON
    # itself is guarded. Same for the situs parts the update nulls.
    sql = " ".join(str(_mod._PARCEL_UPDATE).split())
    for guard in ("property_city IS NOT DISTINCT FROM :old_city",
                  "property_state IS NOT DISTINCT FROM :old_state",
                  "property_zip IS NOT DISTINCT FROM :old_zip",
                  "CAST(enrichment_data AS text) IS NOT DISTINCT FROM :old_enrichment_text"):
        assert guard in sql


def test_candidates_read_the_json_as_text_for_that_guard():
    # Re-serializing the parsed dict would not match Postgres's own rendering, so
    # the guard value must come from the database as text.
    sql = " ".join(str(_mod._PARCEL_CANDIDATES).split())
    assert "CAST(r.enrichment_data AS text) AS enrichment_text" in sql


def test_recover_update_guards_every_value_it_overwrites():
    # Codex P2: it replaces the mailing address and nulls the situs, so those must
    # be guarded too — not just the property address.
    sql = " ".join(str(_mod._PARCEL_RECOVER).split())
    for guard in ("mailing_address IS NOT DISTINCT FROM :old_mailing",
                  "property_city IS NOT DISTINCT FROM :old_city",
                  "property_state IS NOT DISTINCT FROM :old_state",
                  "property_zip IS NOT DISTINCT FROM :old_zip",
                  "CAST(enrichment_data AS text) IS NOT DISTINCT FROM :old_enrichment_text"):
        assert guard in sql
    assert "SET parcel_id" not in sql


def test_recovery_repoints_the_trace_instead_of_cancelling_it():
    # Codex P2: backfill_skip_trace_jobs excludes any result that already has a
    # pending row WHATEVER its status, so cancelling strands the corrected lead
    # forever. Recovery gives it a REAL address, so re-point and re-queue.
    sql = " ".join(str(_mod._REPOINT_PENDING).split())
    assert "SET property_address = :property_address" in sql
    assert "status = 'queued'" in sql
    assert "status IN ('queued', 'errored')" in sql
    assert "property_address IS DISTINCT FROM :property_address" in sql   # idempotent
    requeue = " ".join(str(_mod._REQUEUE_RESULT_TRACE).split())
    assert "SET skip_trace_status = 'queued'" in requeue
    assert "skip_trace_status IN ('not_attempted', 'errored')" in requeue
