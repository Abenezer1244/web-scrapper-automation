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
    assert decide("S", OLD, None) == ("write", None, "none_no_source", "cleared_no_source")


def test_king_found_is_written_even_when_it_equals_the_situs():
    lk = {"mailing_lookup": "found", "mailing_address": OLD}
    # written (real owner-occupied evidence) but recorded as decided, not resolved-new
    assert decide("K", OLD, lk) == ("write", OLD, "king_assessor_tax_bill", "confirmed_same")


def test_king_found_different_address_is_resolved():
    lk = {"mailing_lookup": "found", "mailing_address": "1 MAIN ST, SALEM, MA 01970"}
    assert decide("K", OLD, lk) == (
        "write", lk["mailing_address"], "king_assessor_tax_bill", "resolved")


def test_king_none_means_source_says_no_mailing():
    assert decide("K", OLD, {"mailing_lookup": "none", "mailing_address": None}) == (
        "write", None, "none_king_assessor_no_mailing_block", "cleared_no_source")


def test_king_error_or_missing_is_retried_not_terminal():
    for lk in ({"mailing_lookup": "error", "mailing_address": None},
               {"mailing_lookup": "not_attempted", "mailing_address": None},
               None):
        action, _, _, status = decide("K", OLD, lk)
        assert action == "skip"
        # transient: must stay selectable, or a recoverable outage is written off
        assert status == "retry_later"
        assert status not in _mod._TERMINAL
    assert decide("K", OLD, None)[2] == "king_lookup_missing"


def test_pierce_confirm_vs_refresh_vs_unverified():
    same = {"mailing_address": "712 143RD PL SW,  LYNNWOOD, WA  98087-6429"}
    assert decide("P", OLD, same) == ("confirm", OLD, "pierce_county_gis", "confirmed_same")
    # county dropped the ZIP+4 → same place; keep the richer stored value
    zip5 = {"mailing_address": "712 143RD PL SW, LYNNWOOD, WA, 98087"}
    assert decide("P", OLD, zip5) == ("confirm", OLD, "pierce_county_gis", "confirmed_same")
    diff = {"mailing_address": "PO BOX 4416, SPANAWAY, WA, 98387-4027"}
    assert decide("P", OLD, diff) == (
        "write", diff["mailing_address"], "pierce_county_gis", "resolved")
    # the layer answered and has no such parcel — settled, not transient
    assert decide("P", OLD, None) == (
        "skip", None, "pierce_parcel_not_in_county_layer", "not_found")


def test_every_outcome_leaves_a_durable_status():
    """No decision may leave a row unstamped, or it pins the ordered LIMIT head."""
    cases = [
        ("S", None),
        ("K", {"mailing_lookup": "found", "mailing_address": OLD}),
        ("K", {"mailing_lookup": "found", "mailing_address": "9 A ST, KENT, WA 98032"}),
        ("K", {"mailing_lookup": "none", "mailing_address": None}),
        ("K", {"mailing_lookup": "error", "mailing_address": None}),
        ("K", None),
        ("P", {"mailing_address": OLD}),
        ("P", {"mailing_address": "PO BOX 1, KENT, WA 98032"}),
        ("P", None),
    ]
    for rule, lk in cases:
        status = decide(rule, OLD, lk)[3]
        assert status, f"{rule}/{lk} produced no status"
        assert status in set(_mod._TERMINAL) | {"retry_later"}


def test_the_owner_occupied_case_is_terminal():
    """The exact shape that made 6 prod runs re-write the same 39 rows for ever."""
    lk = {"mailing_lookup": "found", "mailing_address": OLD}
    assert decide("K", OLD, lk)[3] in _mod._TERMINAL
    assert decide("P", OLD, {"mailing_address": OLD})[3] in _mod._TERMINAL


def test_provenance_only_stamped_when_the_address_was_determined():
    determined = {"resolved", "confirmed_same", "cleared_no_source"}
    assert set(_mod._PROVENANCE_STATUSES) == determined
    # a not_found / retry_later row must not claim a mailing_source
    assert decide("P", OLD, None)[3] not in _mod._PROVENANCE_STATUSES
    assert decide("K", OLD, {"mailing_lookup": "error"})[3] not in _mod._PROVENANCE_STATUSES


def test_sql_guards():
    upd = str(_mod._UPDATE)
    assert "mailing_address = :old" in upd
    assert "property_address = :old_property" in upd          # both-address guard
    assert "mailing_source" in upd
    assert "mailing_backfill_status" in upd
    assert "jsonb_typeof" in upd                               # non-object guard
    assert ")::json" in upd                                    # cast back: column is JSON
    stamp = str(_mod._STAMP)
    assert "mailing_backfill_status" in stamp
    assert "mailing_address = :mailing" not in stamp           # stamp never moves the address


def test_candidate_query_excludes_decided_rows_and_avoids_like_metacharacters():
    cand = str(_mod._CANDIDATES)
    assert "LIKE" not in cand.upper().replace("LIKELY", "")     # no wildcard metacharacters
    assert "left(upper(r.mailing_address), length(r.property_address))" in cand
    assert "mailing_backfill_status" in cand
    assert "<> ALL(:terminal)" in cand
    assert "btrim(r.property_address) <> ''" in cand
    # the structured situs + config state must be selected, or the flags stay NULL
    for col in ("r.property_city", "r.property_zip", "sc.state"):
        assert col in cand


def test_terminal_set_is_exactly_the_decided_states():
    assert set(_mod._TERMINAL) == {
        # decided against a real source
        "resolved", "confirmed_same", "cleared_no_source", "not_found",
        # transient failure that exhausted its retries — terminal so it stops
        # holding a slot in the ordered head
        "failed_terminal",
    }
    assert "retry_later" not in _mod._TERMINAL


def test_retry_rows_cannot_pin_the_head_for_ever():
    """A row that fails every time must sort last and eventually go terminal."""
    cand = str(_mod._CANDIDATES)
    # retries sort AFTER untried rows, so untried work is never starved
    assert "= 'retry_later')" in cand
    assert "mailing_backfill_attempts" in cand
    # and the exhausted state is terminal, so it leaves the candidate set for good
    assert "failed_terminal" in _mod._TERMINAL


def test_stale_provenance_is_deleted_not_merged():
    """jsonb || merges; a re-decided row must not keep a mailing_source it lost."""
    upd = str(_mod._UPDATE)
    assert "- 'mailing_source' - 'mailing_backfill_error'" in upd
    assert "mailing_backfill_attempts" in upd


def test_repair_compares_all_four_flags():
    rep = str(_mod._REPAIR)
    for col in ("r.property_state", "r.owner_state", "r.absentee_owner", "r.out_of_state_owner"):
        assert col in rep, col
    # and it must be able to match rule-S rows whose mailing was set to NULL
    assert "mailing_address IS NOT DISTINCT FROM :old" in str(_mod._REPAIR_UPDATE)
