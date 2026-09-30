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


def _sql_without_comments(stmt):
    """Statement text with -- comments stripped, so an assertion cannot pass or
    fail on prose that merely explains the SQL."""
    import re as _re
    body = _re.sub("--.*", " ", str(stmt))
    return " ".join(body.split())


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
                  "CAST(enrichment_data AS text) IS NOT DISTINCT FROM :old_enrichment_text",
                  # The recovered parcel was chosen using the party (owner match).
                  "party_name IS NOT DISTINCT FROM :old_party"):
        assert guard in _sql_without_comments(_mod._PARCEL_RECOVER), guard
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


def test_repoint_rebuilds_the_whole_pending_payload():
    # Codex P1: the dispatcher submits these columns verbatim, so a stale locality,
    # mailing or name from the WRONG parcel would ship a corrected street with a
    # stranger's context. Everything not verified for the corrected parcel is NULL.
    sql = " ".join(str(_mod._REPOINT_PENDING).split())
    for col in ("city = NULL", "state = NULL", "zip = NULL",
                "mail_city = NULL", "mail_state = NULL", "mail_zip = NULL"):
        assert col in sql, col
    assert "mail_address = :mail_address" in sql
    # 1b-1b-i C4: a re-point never clears submission evidence, because it only
    # ever touches a row that has none (see test_repoint_never_revives_a_submitted_row).
    assert "tracerfy_queue_id = NULL" not in sql
    # Names are recomputed rather than nulled — see the dedicated test below.
    requeue = " ".join(str(_mod._REQUEUE_RESULT_TRACE).split())
    assert "SET skip_trace_status = 'queued'" in requeue
    assert "skip_trace_status IN ('not_attempted', 'errored')" in requeue


def test_repoint_never_touches_the_lead_s_name():
    # Codex P1 (rounds 4+5): blanking first/last shipped a 'normal' trace with no
    # name, and re-deriving them via person_tokens() — which is explicitly NOT a
    # surname splitter — turned "VAN DYKE MARY" into last='VAN' first='DYKE'.
    # Names describe the PERSON, which a parcel correction does not change, so the
    # repair leaves them exactly as the enqueue set them.
    sql = " ".join(str(_mod._REPOINT_PENDING).split())
    for assignment in ("first_name =", "last_name =",
                       "first_name IS DISTINCT", "last_name IS DISTINCT"):
        assert assignment not in sql, assignment
    assert not hasattr(_mod, "_party_name_parts")


def test_repoint_also_completes_a_half_fixed_row():
    # Codex P1: guarding only on the street meant a row a PREVIOUS narrower
    # re-point had already street-corrected kept its stale locality/mailing/names.
    sql = " ".join(str(_mod._REPOINT_PENDING).split())
    for cond in ("mail_address IS DISTINCT FROM :mail_address",
                 "city IS NOT NULL", "state IS NOT NULL", "zip IS NOT NULL",
                 "mail_city IS NOT NULL", "mail_state IS NOT NULL", "mail_zip IS NOT NULL"):
        assert cond in sql, cond


def test_repoint_never_revives_a_submitted_row():
    # 1b-1b-i C4 (supersedes the earlier "tracerfy_queue_id IS NOT NULL" trigger).
    # Before 2026-09-07 ingest wrote charged-but-unmatched rows as 'errored' WITH a
    # queue id (tracerfy_ingest.py, the 'unmatched' comment). Such a row is errored,
    # carries a queue id, and was PAID FOR; re-pointing it to 'queued' buys the
    # lookup again. Only a row with no submission evidence may be re-pointed, and
    # the lead is requeued only when the re-point matched (rowcount-gated).
    sql = _sql_without_comments(_mod._REPOINT_PENDING)
    assert "AND tracerfy_queue_id IS NULL" in sql
    assert "AND submitted_at IS NULL" in sql
    assert "tracerfy_queue_id IS NOT NULL" not in sql
    # The cancel path resets the lead to 'not_attempted' after it; same guard.
    cancel = _sql_without_comments(_mod._CANCEL_PENDING)
    assert "AND tracerfy_queue_id IS NULL" in cancel
    assert "AND submitted_at IS NULL" in cancel


def test_party_repair_refreshes_the_stale_trace_name():
    # Codex round 6 [P2]: the pending payload snapshots the lead's NAME at enqueue
    # time. When the party repair rewrites party_name, that snapshot is stale — and
    # for this repair class the OLD party was a placeholder or agency, so the
    # queued trace would be submitted for a person like "State Washington" at a
    # real address, at Tracerfy's expense.
    sql = _sql_without_comments(_mod._PENDING_NAME_REFRESH)
    assert "SET first_name = :new_first, last_name = :new_last, trace_type = :new_trace_type" in sql
    # Only a row that has NOT reached the provider.
    assert "AND status = 'queued'" in sql
    assert "tracerfy_queue_id IS NULL" in sql
    assert "submitted_at IS NULL" in sql
    for st in ("submitting", "submitted", "completed", "errored"):
        assert f"'{st}'" not in sql, st
    # Guarded on every value it read, and a no-op once already correct.
    for guard in ("first_name IS NOT DISTINCT FROM :old_first",
                  "last_name IS NOT DISTINCT FROM :old_last",
                  "trace_type IS NOT DISTINCT FROM :old_trace_type",
                  "first_name IS DISTINCT FROM :new_first"):
        assert guard in sql, guard


def test_trace_name_uses_the_enqueues_own_derivation():
    # Never a bespoke splitter: two ad-hoc ones in this session both got compound
    # surnames wrong. select_traceable_owner is what the enqueue itself uses.
    src = _SCRIPT.read_text(encoding="utf-8")
    assert "select_traceable_owner(new_party)" in src
    # The bespoke splitters are deliberately NOT used for this decision; they
    # are named only in comments explaining why.
    # ...and the same normal/advanced rule the enqueue applies.
    assert '"normal" if (first and last) else "advanced"' in src


# ── the contact-lookup action gate (Phase 1b-2, 2-0 / W2), on REAL rows ─────────
#
# The script writes skip-trace state, so it must serialise with the claim
# (lock_job_for_claim) and must never rewrite a lead a contact-lookup action owns.
# These run against the test database; no network (the parcel writes are driven
# directly, past the live county lookups that decide them).

import threading  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402

from sqlalchemy import text  # noqa: E402

from src.db.session import system_sync_session  # noqa: E402
from src.workers.skip_trace_claim import lock_job_for_claim  # noqa: E402

_BAD_PIN = "12345"  # not a well-formed 10-digit King PIN: a parcel candidate


def _seed_lead(user_id, *, pending=False, action_on_pending=False, verdict=None,
               record_type="probate", party="SMITH JOHN", heirs=None,
               trace=(None, None, "advanced")):
    """A King lead with a malformed parcel. Optionally a queued pending row (itself
    optionally owned by an action) and/or an action verdict on the lead."""
    sc, job, rid = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    with system_sync_session() as db:
        db.execute(text(
            "INSERT INTO scraper_configs (id, user_id, name, county, state, record_type, "
            "fields, enrichment, schedule, deliver, skip_trace_enabled) VALUES "
            "(:sc, :u, 'repair', 'king', 'WA', :rt, '[]', '[]', '{}', '{}', false)"
        ), {"sc": sc, "u": user_id, "rt": record_type})
        db.execute(text(
            "INSERT INTO jobs (id, user_id, scraper_config_id, status, trigger) "
            "VALUES (:j, :u, :sc, 'done', 'manual')"
        ), {"j": job, "u": user_id, "sc": sc})
        db.execute(text(
            "INSERT INTO results (id, job_id, user_id, parcel_id, party_name, heirs, "
            "property_address, property_city, property_state, property_zip, "
            "skip_trace_status, is_duplicate, enrichment_data) VALUES "
            "(:r, :j, :u, :pin, :party, :heirs, '9 WRONG ST', 'SEATTLE', 'WA', '98101', "
            ":st, false, '{}')"
        ), {"r": rid, "j": job, "u": user_id, "pin": _BAD_PIN, "party": party,
            "heirs": heirs, "st": "queued" if pending else "not_attempted"})
        action = None
        if action_on_pending or verdict:
            action = str(uuid.uuid4())
            db.execute(text(
                "INSERT INTO contact_lookup_actions (id, user_id, job_id, category, quote_id, "
                "status, unit_price_cents, currency, pricing_version) VALUES "
                "(:a, :u, :j, 'new', :q, 'claimed', 8, 'USD', '2026-06')"
            ), {"a": action, "u": user_id, "j": job, "q": f"q-{action}"})
        if pending:
            db.execute(text(
                "INSERT INTO pending_skip_trace_rows (id, job_id, result_id, user_id, "
                "property_address, first_name, last_name, trace_type, status, action_id) "
                "VALUES (:p, :j, :r, :u, '9 WRONG ST', :f, :l, :t, 'queued', :a)"
            ), {"p": str(uuid.uuid4()), "j": job, "r": rid, "u": user_id,
                "f": trace[0], "l": trace[1], "t": trace[2],
                "a": action if action_on_pending else None})
        if verdict:
            db.execute(text(
                "INSERT INTO contact_lookup_action_results (id, action_id, user_id, "
                "result_id, disposition) VALUES (:id, :a, :u, :r, :d)"
            ), {"id": str(uuid.uuid4()), "a": action, "u": user_id, "r": rid, "d": verdict})
        db.commit()
    return job, rid


def _candidate(db, rid):
    rows = db.execute(_mod._PARCEL_CANDIDATES, {"pin_len": _mod._KING_PIN_DIGITS}).mappings().all()
    db.rollback()
    return next(r for r in rows if str(r["id"]) == rid)  # raw SQL returns uuid.UUID


def _state(rid):
    with system_sync_session() as db:
        res = db.execute(text(
            "SELECT property_address, skip_trace_status, party_name FROM results WHERE id = :r"
        ), {"r": rid}).one()
        pend = db.execute(text(
            "SELECT status, first_name, last_name, trace_type FROM pending_skip_trace_rows "
            "WHERE result_id = :r"
        ), {"r": rid}).all()
    return res, pend


def _clear(db, row, stats, journal, *, apply=True):
    enrichment = {"parcel_lookup": "mismatch"}
    return _mod._guarded_write(db, row, apply=apply, repair="parcel", stats=stats,
                               journal=journal,
                               write=lambda: _mod._clear_row(db, row, enrichment, stats))


def _stats():
    return {"cleared": 0, "traces_cancelled": 0}


async def test_an_unowned_lead_is_repaired_and_its_trace_cancelled(db, business_user, tmp_path):
    _job, rid = _seed_lead(business_user.id, pending=True)
    stats, journal = _stats(), str(tmp_path / "j.jsonl")
    with system_sync_session() as s:
        assert _clear(s, _candidate(s, rid), stats, journal) is True
    res, pend = _state(rid)
    assert res.property_address is None and res.skip_trace_status == "not_attempted"
    assert [p.status for p in pend] == ["errored"]
    assert stats["cleared"] == 1 and stats["traces_cancelled"] == 1
    assert "skipped_action_linked" not in stats


async def test_a_lead_whose_queued_trace_an_action_owns_is_never_touched(db, business_user, tmp_path):
    _job, rid = _seed_lead(business_user.id, pending=True, action_on_pending=True)
    stats, journal = _stats(), tmp_path / "j.jsonl"
    with system_sync_session() as s:
        assert _clear(s, _candidate(s, rid), stats, str(journal)) is False
    res, pend = _state(rid)
    assert res.property_address == "9 WRONG ST" and res.skip_trace_status == "queued"
    assert [p.status for p in pend] == ["queued"]
    assert stats["skipped_action_linked"] == 1 and stats["cleared"] == 0
    assert '"skipped_action_linked"' in journal.read_text(encoding="utf-8")


async def test_a_lead_an_action_has_quoted_is_never_touched(db, business_user, tmp_path):
    """No pending row yet: the action worker may still claim it."""
    _job, rid = _seed_lead(business_user.id, verdict="quoted")
    stats = _stats()
    with system_sync_session() as s:
        assert _clear(s, _candidate(s, rid), stats, str(tmp_path / "j.jsonl")) is False
    res, _ = _state(rid)
    assert res.property_address == "9 WRONG ST"
    assert stats["skipped_action_linked"] == 1


async def test_a_settled_verdict_does_not_block_a_repair(db, business_user, tmp_path):
    """Only an OPEN verdict owns the lead; a finished action's verdict is history."""
    _job, rid = _seed_lead(business_user.id, verdict="answered_hit")
    stats = _stats()
    with system_sync_session() as s:
        assert _clear(s, _candidate(s, rid), stats, str(tmp_path / "j.jsonl")) is True
    res, _ = _state(rid)
    assert res.property_address is None


async def test_the_open_verdicts_are_real_dispositions():
    from src.db.models import CONTACT_LOOKUP_DISPOSITIONS
    assert set(_mod._ACTION_OPEN_VERDICTS) <= set(CONTACT_LOOKUP_DISPOSITIONS)
    assert set(_mod._ACTION_OPEN_VERDICTS) == {"quoted", "newly_queued"}


async def test_the_write_waits_for_the_jobs_claim_lock(db, business_user, tmp_path, monkeypatch):
    """While the scrape enqueue (or the action worker) holds this job's claim lock,
    the repair must wait INSIDE lock_job_for_claim, then act on what it finds.

    The script's lock call is wrapped in a pass-through spy (the REAL function runs)
    so the test proves where the wait happens, not merely that the thread is slow."""
    job, rid = _seed_lead(business_user.id, pending=True)
    entered, returned = threading.Event(), threading.Event()

    def _spy(session, job_id):
        entered.set()
        lock_job_for_claim(session, job_id)
        returned.set()

    monkeypatch.setattr(_mod, "lock_job_for_claim", _spy)
    stats, done = _stats(), {}
    with system_sync_session() as holder:
        lock_job_for_claim(holder, job)

        def _run():
            with system_sync_session() as s:
                done["wrote"] = _clear(s, _candidate(s, rid), stats, str(tmp_path / "j.jsonl"))

        t = threading.Thread(target=_run)
        t.start()
        assert entered.wait(15), "the repair never reached the claim lock"
        time.sleep(1.0)
        assert not returned.is_set(), "the claim lock was granted while another writer held it"
        res, _ = _state(rid)
        assert res.property_address == "9 WRONG ST"
        holder.commit()  # releases the transaction-scoped advisory lock
        assert returned.wait(15)
        t.join(30)
    assert done["wrote"] is True
    res, _ = _state(rid)
    assert res.property_address is None


async def test_the_repair_locks_the_pending_row_before_the_result(
    db, business_user, tmp_path, monkeypatch,
):
    """The queue's lock order is pending rows, then the result (the dispatcher and the
    claim both follow it). The repair writes the result FIRST, so without taking both
    locks up front it would hold the result while waiting on a pending row a dispatcher
    tick holds: a deadlock. Proof: while another session holds the pending row, the
    blocked repair must hold NOTHING on the result yet (a NOWAIT lock on it succeeds)."""
    job, rid = _seed_lead(business_user.id, pending=True)
    past_claim_lock = threading.Event()

    def _spy(session, job_id):
        lock_job_for_claim(session, job_id)
        past_claim_lock.set()

    monkeypatch.setattr(_mod, "lock_job_for_claim", _spy)
    stats, done = _stats(), {}
    with system_sync_session() as holder:
        holder.execute(text("SELECT id FROM pending_skip_trace_rows WHERE result_id = :r FOR UPDATE"),
                       {"r": rid})

        def _run():
            with system_sync_session() as s:
                done["wrote"] = _clear(s, _candidate(s, rid), stats, str(tmp_path / "j.jsonl"))

        t = threading.Thread(target=_run)
        t.start()
        assert past_claim_lock.wait(15)
        time.sleep(1.0)  # the repair is now blocked on the pending row
        assert t.is_alive()
        with system_sync_session() as probe:
            probe.execute(text("SELECT id FROM results WHERE id = :r FOR UPDATE NOWAIT"), {"r": rid})
            probe.rollback()
        holder.commit()
        t.join(30)
    assert done["wrote"] is True


async def test_a_dry_run_reports_an_owned_lead_and_writes_nothing(db, business_user, tmp_path):
    _job, rid = _seed_lead(business_user.id, pending=True, action_on_pending=True)
    stats, journal = _stats(), tmp_path / "j.jsonl"
    with system_sync_session() as s:
        assert _clear(s, _candidate(s, rid), stats, str(journal), apply=False) is False
    res, pend = _state(rid)
    assert res.property_address == "9 WRONG ST" and [p.status for p in pend] == ["queued"]
    assert stats["skipped_action_linked"] == 1
    assert '"would_skip_action_linked"' in journal.read_text(encoding="utf-8")


async def test_the_party_repair_skips_an_owned_lead_and_repairs_the_rest(db, business_user, tmp_path):
    """End to end through repair_party: the recorder placeholder 'PUBLIC' is swapped
    for the decedent on the free lead (and its queued trace re-derived), never on the
    lead an action owns. Both traces were queued under the PLACEHOLDER's name."""
    stale = ("STATE", "WASHINGTON", "normal")
    _j1, free = _seed_lead(business_user.id, pending=True, party="PUBLIC",
                           heirs="REINKE NORMAN LEONARD", trace=stale)
    _j2, owned = _seed_lead(business_user.id, pending=True, action_on_pending=True,
                            party="PUBLIC", heirs="REINKE NORMAN LEONARD", trace=stale)
    with system_sync_session() as s:
        stats = _mod.repair_party(s, apply=True, journal=str(tmp_path / "j.jsonl"))
    free_res, free_pend = _state(free)
    owned_res, owned_pend = _state(owned)
    assert free_res.party_name == "REINKE NORMAN LEONARD"
    # The enqueue's own rule for this name is an address-only (advanced) trace.
    assert (free_pend[0].first_name, free_pend[0].last_name, free_pend[0].trace_type) == (
        None, None, "advanced")
    assert owned_res.party_name == "PUBLIC"
    assert (owned_pend[0].first_name, owned_pend[0].last_name, owned_pend[0].trace_type) == stale
    assert stats["skipped_action_linked"] == 1
