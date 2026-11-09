"""Migration 138: the 24-month privacy-request purge (D5) and the legal holds that stop it.

Real database, one rolled-back transaction per test. Requests are built directly as the
table owner with back-dated events, which is the only way to make a request 24 months old.
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.db.session import sync_engine

_PURGE = "SELECT * FROM privacy_retention_purge(500)"


@pytest.fixture
def conn():
    with sync_engine.connect() as c:
        tx = c.begin()
        try:
            yield c
        finally:
            tx.rollback()


def _q(conn, sql: str, /, **params):
    return conn.execute(text(sql), params)


def _request(conn, *, closed_months_ago: float | None, terminal: str = "closed",
             kind: str = "opt_out") -> str:
    """A request received long ago and, unless closed_months_ago is None, ended that long ago."""
    rid = str(_q(conn, "INSERT INTO privacy_requests (kind, channel, received_at, deadline_at) "
                       "VALUES (:k, 'email', now() - interval '30 months', "
                       "now() - interval '29 months') RETURNING id", k=kind).scalar())
    _q(conn, "INSERT INTO privacy_request_payloads (request_id, contact_encrypted) "
             "VALUES (:r, 'fe1:contact')", r=rid)
    _q(conn, "INSERT INTO privacy_request_events (request_id, seq, event_type, actor, "
             "occurred_at) VALUES (:r, 1, 'received', 'system', now() - interval '30 months')",
       r=rid)
    if closed_months_ago is not None:
        if terminal == "closed":
            _q(conn, "INSERT INTO privacy_request_events (request_id, seq, event_type, "
                     "decision_code, actor, occurred_at) VALUES (:r, 2, 'decided', 'done', "
                     "'system', now() - make_interval(days => :d))",
               r=rid, d=int(closed_months_ago * 30.5))
        _q(conn, "INSERT INTO privacy_request_events (request_id, seq, event_type, actor, "
                 "occurred_at) VALUES (:r, 3, :t, 'system', now() - make_interval(days => :d))",
           r=rid, t=terminal, d=int(closed_months_ago * 30.5))
    return rid


def _rule(conn, rid: str, state: str = "pending") -> str:
    rule = str(uuid.uuid4())
    _q(conn, "INSERT INTO privacy_suppressions (rule_id, request_id, anchor_kind, anchor_hmac, "
             "subject_kind, subject_hmac, scope, key_version, normalization_version, state, "
             "actor_user_id) VALUES (:rule, :r, 'property', :h, 'person', :h, 'subject', 'v1', "
             "'n3', :s, gen_random_uuid())", rule=rule, r=rid, h="e" * 64, s=state)
    return rule


def _exists(conn, table: str, rid: str) -> bool:
    col = "id" if table == "privacy_requests" else "request_id"
    return _q(conn, f"SELECT EXISTS (SELECT 1 FROM {table} WHERE {col} = :r)", r=rid).scalar()


def test_a_request_closed_two_years_ago_without_a_rule_is_deleted_whole(conn):
    rid = _request(conn, closed_months_ago=25)
    _q(conn, _PURGE)
    for table in ("privacy_requests", "privacy_request_payloads", "privacy_request_events"):
        assert not _exists(conn, table, rid), table


def test_a_withdrawn_request_ages_out_the_same_way(conn):
    rid = _request(conn, closed_months_ago=25, terminal="withdrawn")
    _q(conn, _PURGE)
    assert not _exists(conn, "privacy_requests", rid)


def test_a_rule_keeps_the_paperwork_but_not_the_contact(conn):
    rid = _request(conn, closed_months_ago=25)
    rule = _rule(conn, rid)
    _q(conn, _PURGE)
    assert not _exists(conn, "privacy_request_payloads", rid)
    assert _exists(conn, "privacy_requests", rid)
    assert _exists(conn, "privacy_request_events", rid)
    assert _q(conn, "SELECT count(*) FROM privacy_suppressions WHERE rule_id = :r",
              r=rule).scalar() == 1


@pytest.mark.parametrize("closed", [23, None])
def test_a_request_closed_recently_or_never_is_kept(conn, closed):
    rid = _request(conn, closed_months_ago=closed)
    _q(conn, _PURGE)
    assert _exists(conn, "privacy_request_payloads", rid)


def test_a_legal_hold_stops_the_purge_until_it_is_released(conn):
    rid = _request(conn, closed_months_ago=25)
    op = str(uuid.uuid4())
    _q(conn, "INSERT INTO privacy_legal_holds (request_id, action, actor_user_id) "
             "VALUES (:r, 'hold', :o)", r=rid, o=op)
    _q(conn, _PURGE)
    assert _exists(conn, "privacy_request_payloads", rid)
    _q(conn, "INSERT INTO privacy_legal_holds (request_id, action, actor_user_id) "
             "VALUES (:r, 'release', :o)", r=rid, o=op)
    _q(conn, _PURGE)
    assert not _exists(conn, "privacy_requests", rid)


def test_an_unsent_email_stops_the_purge(conn):
    rid = _request(conn, closed_months_ago=25)
    _q(conn, "INSERT INTO privacy_email_outbox (request_id, kind) VALUES (:r, 'verification')",
       r=rid)
    _q(conn, _PURGE)
    assert _exists(conn, "privacy_request_payloads", rid)
    _q(conn, "UPDATE privacy_email_outbox SET sent_at = now() WHERE request_id = :r", r=rid)
    _q(conn, _PURGE)
    assert not _exists(conn, "privacy_requests", rid)


def test_an_unfinished_erasure_stops_the_purge(conn):
    rid = _request(conn, closed_months_ago=25, kind="delete")
    rule = _rule(conn, rid, "pending")
    active = str(_q(conn, "INSERT INTO privacy_suppressions (rule_id, request_id, anchor_kind, "
                          "anchor_hmac, subject_kind, subject_hmac, scope, key_version, "
                          "normalization_version, state, actor_user_id) SELECT rule_id, "
                          "request_id, anchor_kind, anchor_hmac, subject_kind, subject_hmac, "
                          "scope, key_version, normalization_version, 'active', actor_user_id "
                          "FROM privacy_suppressions WHERE rule_id = :r RETURNING id",
                    r=rule).scalar())
    _q(conn, "INSERT INTO privacy_erasures (rule_id, rule_row_id) VALUES (:r, :row)",
       r=rule, row=active)
    _q(conn, _PURGE)
    assert _exists(conn, "privacy_request_payloads", rid)
    _q(conn, "UPDATE privacy_erasures SET first_pass_at = now(), r2_swept_at = now(), "
             "second_pass_at = now(), completed_at = now() WHERE rule_id = :r", r=rule)
    _q(conn, _PURGE)
    assert not _exists(conn, "privacy_request_payloads", rid)
    assert _exists(conn, "privacy_requests", rid)


def test_holds_are_append_only_and_checked(conn):
    rid = _request(conn, closed_months_ago=None)
    hold = _q(conn, "INSERT INTO privacy_legal_holds (request_id, action, actor_user_id) "
                    "VALUES (:r, 'hold', gen_random_uuid()) RETURNING id", r=rid).scalar()
    for sql, state in (("UPDATE privacy_legal_holds SET action = 'release' WHERE id = :h", "BLD24"),
                       ("INSERT INTO privacy_legal_holds (request_id, action, actor_user_id) "
                        "VALUES (:r, 'forget', gen_random_uuid())", "23514")):
        sp = conn.begin_nested()
        with pytest.raises(DBAPIError) as err:
            conn.execute(text(sql), {"h": hold, "r": rid})
        sp.rollback()
        assert getattr(err.value.orig, "pgcode", None) == state


def test_only_an_admin_with_mfa_can_place_a_hold(conn):
    rid = _request(conn, closed_months_ago=None)
    sp = conn.begin_nested()
    with pytest.raises(DBAPIError) as err:
        conn.execute(text("SELECT privacy_hold_request(CAST(:r AS uuid), 'hold', "
                          "gen_random_uuid())"), {"r": rid})
    sp.rollback()
    assert getattr(err.value.orig, "pgcode", None) == "BLD50"


def test_no_role_without_a_grant_reaches_any_of_it(conn):
    probe = f"r_probe_{uuid.uuid4().hex[:8]}"
    conn.execute(text(f"CREATE ROLE {probe} NOLOGIN"))
    for fn in ("privacy_retention_purge(integer)", "privacy_hold_request(uuid, text, uuid)"):
        assert _q(conn, "SELECT has_function_privilege(:r, :f, 'EXECUTE')",
                  r=probe, f=f"public.{fn}").scalar() is False
    assert _q(conn, "SELECT has_table_privilege(:r, 'public.privacy_legal_holds', 'SELECT')",
              r=probe).scalar() is False


def test_the_grant_script_restores_the_purge_grant() -> None:
    body = (Path(__file__).resolve().parent.parent
            / "scripts/provision_rls_roles.sql").read_text(encoding="utf-8")
    assert "GRANT EXECUTE ON FUNCTION public.privacy_retention_purge(integer)" in body
    assert "REVOKE ALL ON privacy_legal_holds FROM PUBLIC, bridgeleads_app, bridgeleads_system;" in body


def test_a_request_with_two_terminal_events_is_kept(conn):
    """terminal_at is the ONE closed/withdrawn event; malformed data fails closed."""
    rid = _request(conn, closed_months_ago=25)
    _q(conn, "INSERT INTO privacy_request_events (request_id, seq, event_type, actor, "
             "occurred_at) VALUES (:r, 4, 'withdrawn', 'system', now() - interval '26 months')",
       r=rid)
    _q(conn, _PURGE)
    assert _exists(conn, "privacy_request_payloads", rid)


def test_a_live_claim_blocks_the_purge_even_on_a_sent_row(conn):
    rid = _request(conn, closed_months_ago=25)
    _q(conn, "INSERT INTO privacy_email_outbox (request_id, kind, sent_at, claimed_until) "
             "VALUES (:r, 'verification', now(), now() + interval '10 minutes')", r=rid)
    _q(conn, _PURGE)
    assert _exists(conn, "privacy_request_payloads", rid)
