"""Migration 136: the operator functions that record, activate, lift and release rules.

Real database, one rolled-back transaction per test. Expected failures run inside a
SAVEPOINT and assert the function's own SQLSTATE (BLD50-BLD56), so a test cannot pass on
an unrelated error. Role checks SKIP where the runtime roles are absent (CI).
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.db.session import sync_engine
from src.privacy.authorize import load_rule_snapshot

_H = "c" * 64


@pytest.fixture
def conn():
    with sync_engine.connect() as c:
        tx = c.begin()
        try:
            yield c
        finally:
            tx.rollback()


def _id() -> str:
    return str(uuid.uuid4())


def _sqlstate(conn, sql: str, **params) -> str | None:
    sp = conn.begin_nested()
    try:
        conn.execute(text(sql), params)
    except DBAPIError as exc:
        sp.rollback()
        return getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    sp.rollback()
    return None


def _user(conn, *, admin: bool = True, mfa: bool = True) -> str:
    uid = _id()
    conn.execute(text(
        "INSERT INTO users (id, email, password_hash, email_hmac, is_admin, mfa_enabled) "
        "VALUES (:i, :e, 'x', :h, :a, :m)"),
        {"i": uid, "e": f"{uid}@example.test", "h": uuid.uuid4().hex, "a": admin, "m": mfa})
    return uid


def _request(conn, op: str, kind: str = "opt_out") -> str:
    return str(conn.execute(text(
        "SELECT privacy_record_request(:k, 'email', now() + interval '45 days', NULL, "
        "'fe1:contact', NULL, CAST(:a AS uuid))"), {"k": kind, "a": op}).scalar())


_EVENT = ("SELECT privacy_record_event(CAST(:r AS uuid), :t, :v, :d, NULL, CAST(:a AS uuid))")
_ADD = ("SELECT privacy_add_rule(CAST(:r AS uuid), :ak, :h, 'person', :h, :kv, 'n3', "
        "CAST(:a AS uuid), CAST(:rule AS uuid))")
_ACTIVATE = "SELECT privacy_activate_rule(CAST(:rule AS uuid), CAST(:a AS uuid))"
_LIFT = "SELECT * FROM privacy_lift_rule(CAST(:rule AS uuid), CAST(:a AS uuid))"
_RELEASE = ("SELECT privacy_release_review(CAST(:res AS uuid), CAST(:rule AS uuid), "
            "CAST(:a AS uuid))")


def _event(conn, rid: str, op: str, event_type: str, **over) -> None:
    conn.execute(text(_EVENT), {"r": rid, "t": event_type, "v": over.get("v"),
                                "d": over.get("d"), "a": op})


def _rule(conn, rid: str, op: str, *, anchor: str = "property", kv: str = "v1",
          rule: str | None = None) -> str:
    return str(conn.execute(text(_ADD), {"r": rid, "ak": anchor, "h": _H, "kv": kv, "a": op,
                                         "rule": rule}).scalar())


def _verified_rule(conn, op: str, kind: str = "opt_out") -> tuple[str, str]:
    rid = _request(conn, op, kind)
    _event(conn, rid, op, "verified", v="email_link")
    return rid, _rule(conn, rid, op)


def _states(conn, rule: str) -> list[str]:
    return list(conn.execute(text(
        "SELECT state FROM privacy_suppressions WHERE rule_id = :r ORDER BY created_at, state"),
        {"r": rule}).scalars())


def _flagged_result(conn, rule: str) -> tuple[str, str]:
    """A lead row flagged by the rule's active row; returns (result id, owner id)."""
    owner, cfg, job, res = _user(conn, admin=False, mfa=False), _id(), _id(), _id()
    conn.execute(text("INSERT INTO scraper_configs (id, user_id, name, county, state, "
                      "record_type) VALUES (:c, :u, 'P7', 'king', 'WA', 'probate')"),
                 {"c": cfg, "u": owner})
    conn.execute(text("INSERT INTO jobs (id, user_id, scraper_config_id, status) "
                      "VALUES (:j, :u, :c, 'done')"), {"j": job, "u": owner, "c": cfg})
    active = conn.execute(text("SELECT id FROM privacy_suppressions "
                               "WHERE rule_id = :r AND state = 'active' LIMIT 1"),
                          {"r": rule}).scalar()
    conn.execute(text(
        "INSERT INTO results (id, job_id, user_id, party_name, parcel_id, enrichment_data, "
        "dedup_hash, privacy_suppressed_at, privacy_suppression_id, privacy_checked_gen) "
        "VALUES (:i, :j, :u, 'SMITH JOHN', '1234567890', CAST(:ed AS json), :d, now(), :s, 3)"),
        {"i": res, "j": job, "u": owner, "ed": json.dumps({}), "d": uuid.uuid4().hex,
         "s": active})
    return res, str(active)


# ── The operator ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("admin, mfa", [(False, True), (True, False)])
def test_only_an_admin_with_mfa_can_operate(conn, admin, mfa):
    op = _user(conn, admin=admin, mfa=mfa)
    assert _sqlstate(conn, "SELECT privacy_record_request('opt_out', 'email', "
                           "now() + interval '45 days', NULL, 'fe1:c', NULL, "
                           "CAST(:a AS uuid))", a=op) == "BLD50"
    assert _sqlstate(conn, "SELECT privacy_record_request('opt_out', 'email', "
                           "now() + interval '45 days', NULL, 'fe1:c', NULL, NULL)") == "BLD50"


def test_intake_writes_the_envelope_the_payload_and_the_first_event(conn):
    op = _user(conn)
    rid = _request(conn, op)
    assert conn.execute(text("SELECT count(*) FROM privacy_request_payloads "
                             "WHERE request_id = :r"), {"r": rid}).scalar() == 1
    assert conn.execute(text("SELECT event_type, actor, actor_user_id::text "
                             "FROM privacy_request_events WHERE request_id = :r"),
                        {"r": rid}).one() == ("received", "operator", op)


# ── The request lifecycle ──────────────────────────────────────────────────────

def test_events_follow_the_lifecycle(conn):
    op = _user(conn)
    rid = _request(conn, op)
    sql = _EVENT
    assert _sqlstate(conn, sql, r=rid, t="received", v=None, d=None, a=op) == "BLD52"
    assert _sqlstate(conn, sql, r=rid, t="closed", v=None, d=None, a=op) == "BLD52"
    _event(conn, rid, op, "verified", v="email_link")
    assert _sqlstate(conn, sql, r=rid, t="verified", v="email_link", d=None, a=op) == "BLD52"
    _event(conn, rid, op, "decided", d="suppressed")
    assert _sqlstate(conn, sql, r=rid, t="decided", v=None, d="suppressed", a=op) == "BLD52"
    _event(conn, rid, op, "closed")
    assert _sqlstate(conn, sql, r=rid, t="in_review", v=None, d=None, a=op) == "BLD52"
    assert list(conn.execute(text("SELECT seq FROM privacy_request_events "
                                  "WHERE request_id = :r ORDER BY seq"), {"r": rid}).scalars()
                ) == [1, 2, 3, 4]


def test_a_rule_needs_a_verified_undecided_suppression_request(conn):
    op = _user(conn)
    rid = _request(conn, op)
    params = {"r": rid, "ak": "property", "h": _H, "kv": "v1", "a": op, "rule": None}
    assert _sqlstate(conn, _ADD, **params) == "BLD53"          # not verified
    _event(conn, rid, op, "verified", v="email_link")
    assert _sqlstate(conn, _ADD, **params) is None
    _event(conn, rid, op, "decided", d="suppressed")
    assert _sqlstate(conn, _ADD, **params) == "BLD53"          # decided
    access = _request(conn, op, "access")
    _event(conn, access, op, "verified", v="email_link")
    assert _sqlstate(conn, _ADD, **{**params, "r": access}) == "BLD53"


def test_a_mailing_anchor_is_refused(conn):
    op = _user(conn)
    rid = _request(conn, op)
    _event(conn, rid, op, "verified", v="email_link")
    assert _sqlstate(conn, _ADD, r=rid, ak="mailing", h=_H, kv="v1", a=op,
                     rule=None) == "BLD53"


# ── Activation ─────────────────────────────────────────────────────────────────

def test_activation_turns_every_key_version_row_active_once(conn):
    op = _user(conn)
    rid, rule = _verified_rule(conn, op)
    _rule(conn, rid, op, kv="v2", rule=rule)                   # a rotation row
    assert conn.execute(text(_ACTIVATE), {"rule": rule, "a": op}).scalar() == 2
    assert sorted(_states(conn, rule)) == ["active", "active", "pending", "pending"]
    assert conn.execute(text("SELECT count(*) FROM privacy_suppressions WHERE rule_id = :r "
                             "AND state = 'active' AND actor_user_id = :a"),
                        {"r": rule, "a": op}).scalar() == 2
    assert _sqlstate(conn, _ACTIVATE, rule=rule, a=op) == "BLD54"
    assert _sqlstate(conn, _ADD, r=rid, ak="property", h=_H, kv="v3", a=op,
                     rule=rule) == "BLD53"                     # no longer pending
    # An opt-out never queues an erasure.
    assert conn.execute(text("SELECT count(*) FROM privacy_erasures WHERE rule_id = :r"),
                        {"r": rule}).scalar() == 0


def test_a_deletion_rule_queues_its_erasure_and_can_never_be_lifted(conn):
    op = _user(conn)
    _, rule = _verified_rule(conn, op, "delete")
    conn.execute(text(_ACTIVATE), {"rule": rule, "a": op})
    row = conn.execute(text("SELECT e.rule_row_id, s.state FROM privacy_erasures e "
                            "JOIN privacy_suppressions s ON s.id = e.rule_row_id "
                            "WHERE e.rule_id = :r"), {"r": rule}).one()
    assert row.state == "active"
    assert _sqlstate(conn, _LIFT, rule=rule, a=op) == "BLD55"


def test_an_unknown_rule_is_refused(conn):
    op = _user(conn)
    assert _sqlstate(conn, _ACTIVATE, rule=_id(), a=op) == "BLD54"


# ── Lift ───────────────────────────────────────────────────────────────────────

def test_lifting_an_opt_out_releases_every_store_it_held(conn):
    op = _user(conn)
    _, rule = _verified_rule(conn, op)
    assert _sqlstate(conn, _LIFT, rule=rule, a=op) == "BLD55"  # not active yet
    conn.execute(text(_ACTIVATE), {"rule": rule, "a": op})
    res, active = _flagged_result(conn, rule)
    conn.execute(text(
        "INSERT INTO king_parcel_owners (parcel_id, outcome, owner_name, observed_at, source, "
        "privacy_suppression_id) VALUES ('1234567890', 'privacy_withheld', NULL, now(), "
        "'erealproperty', :s)"), {"s": active})
    nts = _id()
    conn.execute(text("INSERT INTO nts_notices (id, source, ts_number, grantor, is_active, "
                      "privacy_suppression_id) VALUES (:i, 'test', :t, NULL, false, :s)"),
                 {"i": nts, "t": nts[:12], "s": active})

    released = [str(r) for r in conn.execute(text(_LIFT), {"rule": rule, "a": op}).scalars()]

    assert released == [res]
    assert conn.execute(text("SELECT privacy_suppressed_at, privacy_suppression_id, "
                             "privacy_checked_gen FROM results WHERE id = :i"),
                        {"i": res}).one() == (None, None, 0)
    assert conn.execute(text("SELECT count(*) FROM king_parcel_owners "
                             "WHERE privacy_suppression_id = :s"), {"s": active}).scalar() == 0
    assert conn.execute(text("SELECT privacy_suppression_id FROM nts_notices WHERE id = :i"),
                        {"i": nts}).scalar() is None
    assert _states(conn, rule).count("lifted") == 1
    assert _sqlstate(conn, _LIFT, rule=rule, a=op) == "BLD55"


# ── Review release ─────────────────────────────────────────────────────────────

def test_a_review_release_is_recorded_and_unflags_only_that_row(conn):
    op = _user(conn)
    _, rule = _verified_rule(conn, op)
    conn.execute(text(_ACTIVATE), {"rule": rule, "a": op})
    res, _ = _flagged_result(conn, rule)
    other, _ = _flagged_result(conn, rule)
    conn.execute(text(_RELEASE), {"res": res, "rule": rule, "a": op})
    assert conn.execute(text("SELECT privacy_suppressed_at FROM results WHERE id = :i"),
                        {"i": res}).scalar() is None
    assert conn.execute(text("SELECT privacy_suppressed_at FROM results WHERE id = :i"),
                        {"i": other}).scalar() is not None
    assert conn.execute(text("SELECT rule_id::text, result_id::text "
                             "FROM privacy_review_release_rows()")).all() == [(rule, res)]
    # The worker's loader carries it into every decision.
    assert load_rule_snapshot(conn).released == {res: frozenset({rule})}
    # Not held by the rule any more: a second release is refused.
    assert _sqlstate(conn, _RELEASE, res=res, rule=rule, a=op) == "BLD56"


# ── Roles ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fn", [
    "privacy_record_request(text, text, timestamptz, text, text, text, uuid)",
    "privacy_activate_rule(uuid, uuid)",
    "privacy_lift_rule(uuid, uuid)",
    "privacy_release_review(uuid, uuid, uuid)",
])
@pytest.mark.parametrize("role", ["bridgeleads_system", "bridgeleads_app", "anon",
                                  "service_role"])
def test_no_runtime_or_api_role_can_call_an_operator_function(conn, fn, role):
    if conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first() \
            is None:
        pytest.skip(f"role {role} absent")
    assert conn.execute(text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                        {"r": role, "f": f"public.{fn}"}).scalar() is False


def test_a_role_with_no_grant_cannot_call_any_of_them(conn):
    """Runs everywhere (the roles above are absent in CI): PUBLIC holds no EXECUTE."""
    probe = f"p7_probe_{uuid.uuid4().hex[:8]}"
    conn.execute(text(f"CREATE ROLE {probe} NOLOGIN"))  # rolled back with the test
    for fn in ("privacy_record_request(text, text, timestamptz, text, text, text, uuid)",
               "privacy_record_event(uuid, text, text, text, timestamptz, uuid)",
               "privacy_add_rule(uuid, text, text, text, text, text, text, uuid, uuid)",
               "privacy_activate_rule(uuid, uuid)", "privacy_lift_rule(uuid, uuid)",
               "privacy_release_review(uuid, uuid, uuid)", "privacy_review_release_rows()"):
        assert conn.execute(text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                            {"r": probe, "f": f"public.{fn}"}).scalar() is False, fn
    assert conn.execute(text("SELECT has_table_privilege(:r, 'public.privacy_review_releases', "
                             "'SELECT')"), {"r": probe}).scalar() is False


def test_only_the_worker_role_reads_the_releases(conn):
    if conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system'")
                    ).first() is None:
        pytest.skip("runtime roles absent")
    fn = "public.privacy_review_release_rows()"
    assert conn.execute(text("SELECT has_function_privilege('bridgeleads_system', :f, "
                             "'EXECUTE')"), {"f": fn}).scalar() is True
    assert conn.execute(text("SELECT has_function_privilege('bridgeleads_app', :f, "
                             "'EXECUTE')"), {"f": fn}).scalar() is False
