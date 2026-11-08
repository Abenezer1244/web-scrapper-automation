"""Migration 135: privacy_erase_batch() erases one batch across every store (Phase 6 P6a).

Real database, one transaction rolled back at the end. The database refuses any rule
state but 'pending' until P7 (125's activation guard), so the test drops that CHECK
inside its own transaction to record an ACTIVE deletion rule; the rollback restores it.
Expected refusals run in a SAVEPOINT. Synthetic people only.
"""
from __future__ import annotations

import json
import re
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.db.session import sync_engine

_CALL = ("SELECT privacy_erase_batch(CAST(:rule AS uuid), CAST(:res AS uuid[]), "
         "CAST(:keys AS text[]), CAST(:king AS text[]), CAST(:nts AS uuid[]), "
         "CAST(:county AS uuid[]))")


def _id() -> str:
    return str(uuid.uuid4())


def _ins(conn, sql: str, **params) -> None:
    conn.execute(text(sql), params)


def _user(conn) -> str:
    uid = _id()
    _ins(conn, "INSERT INTO users (id, email, password_hash, email_hmac) "
               "VALUES (:i, :e, 'x', :h)", i=uid, e=f"{uid}@example.test", h=uuid.uuid4().hex)
    return uid


def _start_purge(conn, uid: str) -> None:
    """Only the purge role may move deletion_state (112's guard)."""
    conn.execute(text("SET LOCAL ROLE bridgeleads_purge"))
    for state in ("pending", "purging"):
        _ins(conn, "UPDATE users SET deletion_state = :s WHERE id = :u", s=state, u=uid)
    conn.execute(text("RESET ROLE"))


def _rule(conn, kind: str, states: tuple[str, ...]) -> str:
    """A rule for a request of `kind`; returns the id of its LAST state row."""
    req = conn.execute(text(
        "INSERT INTO privacy_requests (kind, channel, deadline_at) "
        "VALUES (:k, 'web', now() + interval '45 days') RETURNING id"), {"k": kind}).scalar()
    rule_id, row = _id(), None
    for state in states:
        row = conn.execute(text(
            "INSERT INTO privacy_suppressions (rule_id, request_id, anchor_kind, "
            "anchor_hmac, subject_kind, subject_hmac, scope, key_version, "
            "normalization_version, state) VALUES (:r, :q, 'property', :h, 'person', :h, "
            "'subject', 'v1', 'n3', :s) RETURNING id"),
            {"r": rule_id, "q": req, "h": "c" * 64, "s": state}).scalar()
    return str(row)


def _job(conn, uid: str) -> tuple[str, str]:
    cfg, job = _id(), _id()
    _ins(conn, "INSERT INTO scraper_configs (id, user_id, name, county, state, record_type) "
               "VALUES (:c, :u, 'Erase', 'king', 'WA', 'probate')", c=cfg, u=uid)
    _ins(conn, "INSERT INTO jobs (id, user_id, scraper_config_id, status, export_key) "
               "VALUES (:j, :u, :c, 'done', :k)", j=job, u=uid, c=cfg,
         k=f"exports/{uid}/{job}/leads.csv")
    return cfg, job


def _result(conn, uid: str, job: str, name: str, subject_hash: str | None) -> str:
    rid = _id()
    _ins(conn, "INSERT INTO results (id, job_id, user_id, party_name, heirs, parcel_id, "
               "property_address, mailing_address, enrichment_data, email, "
               "skip_trace_subject_hash, dedup_hash) VALUES (:i, :j, :u, :n, 'HEIR ONE', "
               "'1234567890', '1 TEST ST', '2 MAIL ST', CAST(:ed AS json), 'x@example.test', "
               ":h, :d)",
         i=rid, j=job, u=uid, n=name, ed=json.dumps({"assessor_current_owner": name}),
         h=subject_hash, d=uuid.uuid4().hex)
    return rid


def _sqlstate(conn, params: dict) -> str | None:
    sp = conn.begin_nested()
    try:
        conn.execute(text(_CALL), params)
    except DBAPIError as exc:
        sp.rollback()
        return getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    sp.rollback()
    return None


def _params(rule: str, **over) -> dict:
    base = {"rule": rule, "res": [], "keys": [], "king": [], "nts": [], "county": []}
    base.update(over)
    return base


@pytest.fixture
def conn():
    with sync_engine.connect() as c:
        tx = c.begin()
        c.execute(text("ALTER TABLE privacy_suppressions "
                       "DROP CONSTRAINT ck_privacy_suppressions_activation_guard"))
        try:
            yield c
        finally:
            tx.rollback()


def test_erases_every_store_and_keeps_the_skeleton(conn):
    rule = _rule(conn, "delete", ("pending", "active"))
    uid = _user(conn)
    cfg, job = _job(conn, uid)
    batch = _id()
    _ins(conn, "INSERT INTO scraper_batches (id, user_id, state, fields, enrichment, "
               "schedule, deliver) VALUES (:b, :u, 'WA', '[]', '[]', '{}', '{}')",
         b=batch, u=uid)
    _ins(conn, "INSERT INTO batch_runs (id, batch_id, user_id, child_job_ids, "
               "combined_export_key) VALUES (:i, :b, :u, CAST(:c AS json), 'k')",
         i=_id(), b=batch, u=uid, c=json.dumps([job]))
    target = _result(conn, uid, job, "SMITH JOHN", "a" * 64)
    other = _result(conn, uid, job, "DOE JANE", "b" * 64)
    queued, sent = _id(), _id()
    for pid, status, queue in ((queued, "queued", None), (sent, "completed", 77)):
        _ins(conn, "INSERT INTO pending_skip_trace_rows (id, job_id, result_id, user_id, "
                   "property_address, first_name, last_name, mail_address, status, "
                   "tracerfy_queue_id) VALUES (:i, :j, :r, :u, '1 TEST ST', 'JOHN', 'SMITH', "
                   "'2 MAIL ST', :s, :q)",
             i=pid, j=job, r=target, u=uid, s=status, q=queue)
    _ins(conn, "INSERT INTO skip_trace_queues (id, tracerfy_queue_id, user_id, download_url) "
               "VALUES (:i, 77, :u, 'enc')", i=_id(), u=uid)
    for key in ("a" * 64, "d" * 64, "b" * 64):
        _ins(conn, "INSERT INTO skip_trace_cache (address_hash) VALUES (:k)", k=key)
    # One outbox row per (job, result): the delivered one comes from an earlier push job.
    pending_d, delivered_d = _id(), _id()
    _, push_job = _job(conn, uid)
    for did, status, jid in ((pending_d, "pending", job), (delivered_d, "delivered", push_job)):
        _ins(conn, "INSERT INTO dialer_deliveries (id, job_id, result_id, user_id, "
                   "scraper_config_id, vendor_id, status) VALUES (:i, :j, :r, :u, :c, "
                   "'phoneburner', :s)", i=did, j=jid, r=target, u=uid, c=cfg, s=status)
    _ins(conn, "INSERT INTO king_parcel_owners (parcel_id, outcome, owner_name, observed_at, "
               "source) VALUES ('1234567890', 'found', 'SMITH JOHN', now(), 'erealproperty')")
    nts = _id()
    _ins(conn, "INSERT INTO nts_notices (id, source, ts_number, grantor) "
               "VALUES (:i, 'test', :t, 'SMITH JOHN')", i=nts, t=nts[:12])
    county = _id()
    _ins(conn, "INSERT INTO county_records (id, county, state, record_hash, party_name) "
               "VALUES (:i, 'king', 'WA', :h, 'SMITH JOHN')", i=county, h=uuid.uuid4().hex)

    params = _params(rule, res=[target], keys=["d" * 64], king=["1234567890"], nts=[nts],
                     county=[county])
    counts = conn.execute(text(_CALL), params).scalar()
    assert counts == {"results": 1, "cache": 2, "queues": 1, "pending": 2, "dialer": 1,
                      "jobs": 1, "batch_runs": 1, "king": 1, "nts": 1, "county": 1}

    row = conn.execute(text(
        "SELECT party_name, heirs, mailing_address, enrichment_data, email, "
        "skip_trace_subject_hash, parcel_id, property_address, "
        "privacy_suppressed_at IS NOT NULL, privacy_suppression_id::text, "
        "privacy_erased_at IS NOT NULL FROM results WHERE id = :i"), {"i": target}).one()
    assert row[:6] == (None,) * 6
    assert row[6:] == ("1234567890", "1 TEST ST", True, rule, True)
    assert conn.execute(text("SELECT party_name, privacy_erased_at FROM results "
                             "WHERE id = :i"), {"i": other}).one() == ("DOE JANE", None)
    assert conn.execute(text("SELECT array_agg(address_hash) FROM skip_trace_cache WHERE "
                             "address_hash IN (:a, :b, :d)"),
                        {"a": "a" * 64, "b": "b" * 64, "d": "d" * 64}).scalar() == ["b" * 64]
    pend = dict(conn.execute(text(
        "SELECT id::text, status || '|' || coalesce(first_name, '') || coalesce(mail_address, '') "
        "FROM pending_skip_trace_rows WHERE result_id = :r"), {"r": target}).all())
    assert pend == {queued: "cancelled|", sent: "completed|"}
    assert conn.execute(text("SELECT download_url FROM skip_trace_queues "
                             "WHERE tracerfy_queue_id = 77")).scalar() is None
    dial = dict(conn.execute(text("SELECT id::text, status FROM dialer_deliveries "
                                  "WHERE result_id = :r"), {"r": target}).all())
    assert dial == {pending_d: "skipped", delivered_d: "delivered"}
    assert conn.execute(text("SELECT privacy_invalidated_at IS NOT NULL, export_key "
                             "FROM jobs WHERE id = :j"), {"j": job}).one()[0] is True
    assert conn.execute(text("SELECT outcome, owner_name, privacy_suppression_id::text "
                             "FROM king_parcel_owners WHERE parcel_id = '1234567890'")
                        ).one() == ("privacy_withheld", None, rule)
    assert conn.execute(text("SELECT grantor, is_active FROM nts_notices WHERE id = :i"),
                        {"i": nts}).one() == (None, False)
    assert conn.execute(text("SELECT count(*) FROM county_records WHERE id = :i"),
                        {"i": county}).scalar() == 0

    # Idempotent: a clean second pass erases nothing.
    again = conn.execute(text(_CALL), params).scalar()
    assert set(again.values()) == {0}


def test_skips_accounts_being_purged(conn):
    rule = _rule(conn, "delete", ("pending", "active"))
    uid = _user(conn)
    _, job = _job(conn, uid)
    rid = _result(conn, uid, job, "SMITH JOHN", None)
    _start_purge(conn, uid)
    counts = conn.execute(text(_CALL), _params(rule, res=[rid])).scalar()
    assert counts["results"] == 0
    assert conn.execute(text("SELECT party_name FROM results WHERE id = :i"),
                        {"i": rid}).scalar() == "SMITH JOHN"


@pytest.mark.parametrize("kind,states", [
    ("delete", ("pending",)),                      # never activated
    ("opt_out", ("pending", "active")),            # opt-out suppresses, never erases
])
def test_refuses_anything_but_an_active_deletion_rule(conn, kind, states):
    assert _sqlstate(conn, _params(_rule(conn, kind, states))) == "BLD41"


def test_refuses_a_lifted_rule(conn):
    active = _rule(conn, "delete", ("pending", "active"))
    rule_id = conn.execute(text("SELECT rule_id, request_id FROM privacy_suppressions "
                                "WHERE id = :i"), {"i": active}).one()
    _ins(conn, "INSERT INTO privacy_suppressions (rule_id, request_id, anchor_kind, "
               "anchor_hmac, subject_kind, subject_hmac, scope, key_version, "
               "normalization_version, state) VALUES (:r, :q, 'property', :h, 'person', :h, "
               "'subject', 'v1', 'n3', 'lifted')", r=rule_id[0], q=rule_id[1], h="c" * 64)
    assert _sqlstate(conn, _params(active)) == "BLD41"


def test_refuses_an_oversized_batch(conn):
    rule = _rule(conn, "delete", ("pending", "active"))
    assert _sqlstate(conn, _params(rule, res=[_id() for _ in range(501)])) == "BLD40"


def test_function_is_owned_by_the_purge_role_and_closed_to_public(conn):
    owner, acl = conn.execute(text(
        "SELECT pg_get_userbyid(p.proowner), coalesce(p.proacl::text, '') FROM pg_proc p "
        "WHERE p.proname = 'privacy_erase_batch'")).one()
    assert owner == "bridgeleads_purge"
    assert not re.search(r"(^|[{,])=X/", acl)  # no PUBLIC execute entry
    reader_acl = conn.execute(text(
        "SELECT coalesce(p.proacl::text, '') FROM pg_proc p "
        "WHERE p.proname = 'privacy_erasure_rule'")).scalar()
    assert not re.search(r"(^|[{,])=X/", reader_acl)
    assert "bridgeleads_purge=X/" in reader_acl
    assert conn.execute(text(
        "SELECT relforcerowsecurity FROM pg_class WHERE relname = 'privacy_erasures'")
    ).scalar() is True
