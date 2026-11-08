"""The privacy erasure driver: each phase, the repeat after a late write, R2 failures, the
lock (Phase 6 P6c/P6d, src/workers/privacy_erasure.py).

Real database. An ACTIVE deletion rule can only exist inside a transaction that drops
125's activation guard, so every session the driver opens is bound to ONE outer
transaction (SQLAlchemy's create_savepoint mode: the driver's commits release
savepoints) and the whole lifecycle is rolled back at the end. R2 is the only stand-in:
an in-memory store recording what was deleted. Synthetic people only.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

import src.db.session as db_session
from src.db.models import Job, Result, ScraperConfig, User
from src.db.session import sync_engine
from src.workers.privacy_erasure import SECOND_PASS_DELAY, drive_privacy_erasures
from tests.test_privacy_authorize import _history, _keyring  # noqa: F401 -- autouse keyring

_PARCEL = "1234567890"


class _Store:
    def __init__(self, fail: bool = False):
        self.deleted: list[str] = []
        self.fail = fail

    def delete(self, key: str) -> bool:
        if self.fail:
            return False
        self.deleted.append(key)
        return True


@pytest.fixture
def conn(monkeypatch):
    with sync_engine.connect() as c:
        outer = c.begin()
        # Nothing else in this database may hold the driver's phases up: earlier runs'
        # unfinished jobs and lookups, and other erasures (all rolled back below).
        c.execute(text("UPDATE jobs SET status = 'done' "
                       "WHERE status NOT IN ('done', 'failed', 'cancelled')"))
        c.execute(text("DELETE FROM privacy_erasures"))

        @contextmanager
        def _session():
            s = Session(bind=c, join_transaction_mode="create_savepoint")
            try:
                yield s
            finally:
                s.close()

        monkeypatch.setattr(db_session, "system_sync_session", _session)
        try:
            yield c
        finally:
            outer.rollback()


def _q(c, sql: str, **params):
    return c.execute(text(sql), params)


def _rule(c, kind: str = "delete") -> tuple[str, str]:
    req = _q(c, "INSERT INTO privacy_requests (kind, channel, deadline_at) VALUES "
                "(:k, 'web', now() + interval '45 days') RETURNING id", k=kind).scalar()
    rows = _history(["pending", "active"])
    for r in rows:
        _q(c, "INSERT INTO privacy_suppressions (id, rule_id, request_id, anchor_kind, "
              "anchor_hmac, subject_kind, subject_hmac, scope, key_version, "
              "normalization_version, state, created_at) VALUES (:id, :rule_id, :req, "
              ":anchor_kind, :anchor_hmac, :subject_kind, :subject_hmac, :scope, "
              ":key_version, :normalization_version, :state, :created_at)", **r, req=req)
    rule_id, row = rows[1]["rule_id"], rows[1]["id"]
    _q(c, "INSERT INTO privacy_erasures (rule_id, rule_row_id) VALUES (:r, :w)",
       r=rule_id, w=row)
    return rule_id, row


def _lead(c, name: str = "SMITH JOHN") -> dict:
    with Session(bind=c, join_transaction_mode="create_savepoint") as s:
        uid, cfg, job, rid = (str(uuid.uuid4()) for _ in range(4))
        s.add(User(id=uid, email=f"{uid}@example.test", password_hash="x",
                   email_hmac=uuid.uuid4().hex))
        s.flush()
        s.add(ScraperConfig(id=cfg, user_id=uid, name="Erase", county="king", state="WA",
                            record_type="probate", fields=[], enrichment=[],
                            schedule={"frequency": "manual"}, deliver={"emails": []}))
        s.flush()
        s.add(Job(id=job, user_id=uid, scraper_config_id=cfg, status="done",
                  trigger="manual", record_count=1, billed_count=0, retry_count=0,
                  export_key=f"exports/{uid}/{job}/leads.csv"))
        s.flush()
        s.add(Result(id=rid, job_id=job, user_id=uid, party_name=name, parcel_id=_PARCEL,
                     property_address="1 TEST ST", dedup_hash=uuid.uuid4().hex,
                     is_duplicate=False, enrichment_data={}))
        s.commit()
    return {"user": uid, "job": job, "result": rid}


def _state(c, rule_id: str):
    return _q(c, "SELECT first_pass_at, r2_swept_at, second_pass_at, completed_at, "
                 "attempts, last_error FROM privacy_erasures WHERE rule_id = :r",
              r=rule_id).one()


def _tick(store, now: datetime) -> dict:
    return drive_privacy_erasures(store=store, now=now)["phases"]


def test_an_empty_table_is_one_query(conn):
    assert drive_privacy_erasures(store=_Store()) == {"open": 0, "phases": {}}


def test_an_erasure_runs_every_phase_to_completion(conn):
    rule_id, _ = _rule(conn)
    lead = _lead(conn)
    other = _lead(conn, "DOE JANE")
    ready = _q(conn, "INSERT INTO account_exports (user_id, status, size_bytes, ready_at, "
                     "expires_at) VALUES (:u, 'ready', 10, now(), now() + interval '7 days') "
                     "RETURNING id::text", u=lead["user"]).scalar()
    store = _Store()
    t0 = datetime.now(UTC)

    assert _tick(store, t0) == {rule_id: "first_pass_done"}
    assert _q(conn, "SELECT party_name FROM results WHERE id = :i",
              i=lead["result"]).scalar() is None
    assert store.deleted == []                       # nothing leaves R2 before the sweep

    assert _tick(store, t0) == {rule_id: "r2_swept"}
    assert sorted(store.deleted) == sorted([
        f"exports/{lead['user']}/{lead['job']}/leads.csv",
        f"exports/{lead['user']}/account/{ready}.zip"])
    assert f"exports/{other['user']}/{other['job']}/leads.csv" not in store.deleted
    assert _q(conn, "SELECT status FROM account_exports WHERE id = :i",
              i=ready).scalar() == "expired"

    assert _tick(store, t0 + timedelta(hours=1)) == {rule_id: "second_pass_waiting"}
    later = t0 + SECOND_PASS_DELAY + timedelta(minutes=1)
    assert _tick(store, later) == {rule_id: "second_pass_done"}
    assert _tick(store, later) == {rule_id: "completed"}
    assert _state(conn, rule_id).completed_at is not None
    assert _tick(store, later) == {}                 # nothing open any more


def test_a_late_write_sends_the_erasure_round_again(conn):
    rule_id, _ = _rule(conn)
    _lead(conn)
    store = _Store()
    t0 = datetime.now(UTC)
    _tick(store, t0)
    _tick(store, t0)                                 # first pass, R2 sweep
    late = _lead(conn)                               # an in-flight job wrote the name back
    later = t0 + SECOND_PASS_DELAY + timedelta(minutes=1)
    assert _tick(store, later) == {rule_id: "repeat"}
    st = _state(conn, rule_id)
    assert st.r2_swept_at is None and st.second_pass_at is None
    assert _q(conn, "SELECT party_name FROM results WHERE id = :i",
              i=late["result"]).scalar() is None
    # The late lead's export is swept on the next tick; then another full 24 h wait.
    assert _tick(store, later) == {rule_id: "r2_swept"}
    assert f"exports/{late['user']}/{late['job']}/leads.csv" in store.deleted
    assert _tick(store, later) == {rule_id: "second_pass_waiting"}


def test_a_failed_r2_delete_is_recorded_and_never_marked_done(conn):
    rule_id, _ = _rule(conn)
    _lead(conn)
    t0 = datetime.now(UTC)
    _tick(_Store(), t0)
    assert _tick(_Store(fail=True), t0) == {rule_id: "error"}
    st = _state(conn, rule_id)
    assert (st.r2_swept_at, st.attempts, st.last_error) == (None, 1, "RuntimeError")
    assert _tick(_Store(), t0) == {rule_id: "r2_swept"}   # retried next tick


def test_an_account_export_still_building_holds_the_sweep(conn):
    rule_id, _ = _rule(conn)
    lead = _lead(conn)
    _q(conn, "INSERT INTO account_exports (user_id, status) VALUES (:u, 'building')",
       u=lead["user"])
    t0 = datetime.now(UTC)
    _tick(_Store(), t0)
    assert _tick(_Store(), t0) == {rule_id: "r2_waiting"}


def test_an_opt_out_rule_is_refused_by_the_database(conn):
    rule_id, _ = _rule(conn, kind="opt_out")
    _lead(conn)
    assert _tick(_Store(), datetime.now(UTC)) == {rule_id: "error"}
    assert _state(conn, rule_id).first_pass_at is None


def test_a_second_driver_skips_while_one_holds_the_lock(conn):
    with sync_engine.connect() as other:
        assert other.execute(text("SELECT pg_try_advisory_lock(7135000001)")).scalar()
        try:
            assert drive_privacy_erasures(store=_Store()) == {"skipped": True}
        finally:
            other.execute(text("SELECT pg_advisory_unlock(7135000001)"))


def test_an_export_uploaded_late_by_an_in_flight_job_is_swept_before_completion(conn):
    """Codex P6d: a job running at the first pass builds its export from data it read
    before the erasure and uploads it after the first sweep."""
    rule_id, _ = _rule(conn)
    lead = _lead(conn)
    _q(conn, "UPDATE jobs SET status = 'running', export_key = NULL WHERE id = :j",
       j=lead["job"])
    store = _Store()
    t0 = datetime.now(UTC)
    assert _tick(store, t0) == {rule_id: "first_pass_done"}
    assert _tick(store, t0) == {rule_id: "r2_swept"}
    later = t0 + SECOND_PASS_DELAY + timedelta(minutes=1)
    assert _tick(store, later) == {rule_id: "second_pass_waiting"}   # the job still runs
    late_key = f"exports/{lead['user']}/{lead['job']}/leads.csv"
    _q(conn, "UPDATE jobs SET status = 'done', export_key = :k WHERE id = :j",
       k=late_key, j=lead["job"])
    assert late_key not in store.deleted
    assert _tick(store, later) == {rule_id: "second_pass_done"}
    assert _tick(store, later) == {rule_id: "completed"}
    assert late_key in store.deleted


def test_a_running_batch_holds_the_second_pass(conn):
    rule_id, _ = _rule(conn)
    lead = _lead(conn)
    batch = str(uuid.uuid4())
    _q(conn, "INSERT INTO scraper_batches (id, user_id, state, fields, enrichment, schedule, "
             "deliver) VALUES (:b, :u, 'WA', '[]', '[]', '{}', '{}')", b=batch, u=lead["user"])
    run = _q(conn, "INSERT INTO batch_runs (id, batch_id, user_id, status, child_job_ids) "
                   "VALUES (gen_random_uuid(), :b, :u, 'running', CAST(:kids AS json)) "
                   "RETURNING id::text",
             b=batch, u=lead["user"], kids=f'["{lead["job"]}"]').scalar()
    store = _Store()
    t0 = datetime.now(UTC)
    _tick(store, t0)
    _tick(store, t0)
    later = t0 + SECOND_PASS_DELAY + timedelta(minutes=1)
    assert _tick(store, later) == {rule_id: "second_pass_waiting"}
    combined = f"exports/{lead['user']}/batch/{run}/combined.csv"
    _q(conn, "UPDATE batch_runs SET status = 'done', combined_export_key = :k WHERE id = :r",
       k=combined, r=run)
    assert _tick(store, later) == {rule_id: "second_pass_done"}
    assert _tick(store, later) == {rule_id: "completed"}
    assert combined in store.deleted


def test_a_lookup_still_out_at_tracerfy_holds_completion(conn):
    rule_id, _ = _rule(conn)
    lead = _lead(conn)
    pid = _q(conn, "INSERT INTO pending_skip_trace_rows (id, job_id, result_id, user_id, "
                   "property_address, status, tracerfy_queue_id) VALUES (gen_random_uuid(), "
                   ":j, :r, :u, '1 TEST ST', 'submitted', 991) RETURNING id",
             j=lead["job"], r=lead["result"], u=lead["user"]).scalar()
    store = _Store()
    t0 = datetime.now(UTC)
    later = t0 + SECOND_PASS_DELAY + timedelta(minutes=1)
    for now in (t0, t0, later):
        _tick(store, now)
    assert _tick(store, later) == {rule_id: "tracerfy_waiting"}
    _q(conn, "UPDATE pending_skip_trace_rows SET status = 'quarantined' WHERE id = :i", i=pid)
    assert _tick(store, later) == {rule_id: "completed"}


def test_an_older_job_retried_after_the_second_pass_holds_the_final_sweep(conn):
    """Codex P6d re-review: the barrier is re-checked at the final sweep."""
    rule_id, _ = _rule(conn)
    lead = _lead(conn)
    store = _Store()
    t0 = datetime.now(UTC)
    later = t0 + SECOND_PASS_DELAY + timedelta(minutes=1)
    for now in (t0, t0, later):
        _tick(store, now)                       # first pass, sweep, second pass
    _q(conn, "UPDATE jobs SET status = 'running' WHERE id = :j", j=lead["job"])  # retried
    assert _tick(store, later) == {rule_id: "final_r2_waiting"}
    _q(conn, "UPDATE jobs SET status = 'done' WHERE id = :j", j=lead["job"])
    assert _tick(store, later) == {rule_id: "completed"}
