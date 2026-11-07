"""Migration 129: workers read suppression rules through privacy_rule_rows() (P4a).

The production worker role is NOSUPERUSER NOBYPASSRLS and holds no privilege on
privacy_suppressions (migration 125). These tests stand in a throwaway role with the
same attributes: it must read every rule row through the definer function, must still
be refused the table itself, and must see only the loader's columns. Each test runs in
one transaction that is rolled back (the probe role included).
"""
from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from src.db.session import sync_engine
from src.privacy.authorize import load_rule_snapshot
from tests.test_privacy_schema import _RULE, _probe_role, _request, _rule, _sqlstate

_LOADER_COLUMNS = ["id", "rule_id", "anchor_kind", "anchor_hmac", "subject_kind",
                   "subject_hmac", "scope", "key_version", "normalization_version", "state",
                   "created_at"]


@pytest.fixture
def conn():
    with sync_engine.connect() as c:
        trans = c.begin()
        try:
            yield c
        finally:
            trans.rollback()


def _reader(conn) -> str:
    role = _probe_role(conn)
    conn.execute(text(f"GRANT EXECUTE ON FUNCTION public.privacy_rule_rows() TO {role}"))
    return role


def test_a_nobypassrls_reader_sees_every_rule_row_but_not_the_table(conn) -> None:
    rid = _request(conn)
    rules = [_rule(rid), _rule(rid, ak="phone", sk="person")]
    for r in rules:
        conn.execute(text(_RULE), r)
    role = _reader(conn)
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    seen = {r for (r,) in conn.execute(text("SELECT rule_id FROM public.privacy_rule_rows()"))}
    assert seen >= {r["rule"] for r in rules}
    assert _sqlstate(conn, "SELECT 1 FROM privacy_suppressions LIMIT 1") == "42501"
    assert _sqlstate(conn, "SELECT 1 FROM privacy_requests LIMIT 1") == "42501"
    conn.execute(text("RESET ROLE"))


def test_the_loader_runs_as_that_reader(conn) -> None:
    """load_rule_snapshot itself, under the worker-shaped role: no error, rows read."""
    rid = _request(conn)
    conn.execute(text(_RULE), _rule(rid))
    conn.execute(text(f"SET LOCAL ROLE {_reader(conn)}"))
    snapshot = load_rule_snapshot(conn)  # raises RuleSnapshotError on any refusal
    assert snapshot.active_rules == 0     # pending only: 125's guard allows nothing else
    conn.execute(text("RESET ROLE"))


def test_it_returns_only_the_loaders_columns(conn) -> None:
    cols = conn.execute(text("SELECT * FROM public.privacy_rule_rows() LIMIT 0")).keys()
    assert list(cols) == _LOADER_COLUMNS
    assert "request_id" not in cols


def test_without_the_grant_a_role_cannot_call_it(conn) -> None:
    role = _probe_role(conn)
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    assert _sqlstate(conn, "SELECT 1 FROM public.privacy_rule_rows()") == "42501"
    conn.execute(text("RESET ROLE"))


def test_it_is_a_pinned_definer_function_public_cannot_execute(conn) -> None:
    secdef, config, public_exec = conn.execute(text(
        "SELECT p.prosecdef, p.proconfig, "
        "       has_function_privilege('public', p.oid, 'EXECUTE') "
        "FROM pg_proc p WHERE p.oid = 'public.privacy_rule_rows()'::regprocedure")).one()
    assert secdef is True
    assert "search_path=public, pg_temp" in (config or [])
    assert public_exec is False


def test_its_owner_owns_the_table_and_rls_is_not_forced(conn) -> None:
    """Otherwise the function would see zero rows without an error: allow-all."""
    same_owner, forced = conn.execute(text(
        "SELECT p.proowner = c.relowner, c.relforcerowsecurity "
        "FROM pg_proc p, pg_class c "
        "WHERE p.oid = 'public.privacy_rule_rows()'::regprocedure "
        "  AND c.oid = 'public.privacy_suppressions'::regclass")).one()
    assert same_owner is True
    assert forced is False


@pytest.mark.parametrize("role,expected", [("bridgeleads_system", True),
                                           ("bridgeleads_app", False)])
def test_runtime_roles(conn, role, expected) -> None:
    if not conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first():
        pytest.skip(f"{role} not on this cluster")
    assert conn.execute(text(
        "SELECT has_function_privilege(:r, 'public.privacy_rule_rows()', 'EXECUTE')"),
        {"r": role}).scalar() is expected


def test_the_grant_script_grants_the_function_to_workers_only() -> None:
    # provision_rls_roles.sql re-runs after migrations on a fresh cluster; without the
    # grant there, workers would fail closed and every send would stop.
    body = (Path(__file__).resolve().parent.parent
            / "scripts/provision_rls_roles.sql").read_text(encoding="utf-8")
    assert "GRANT EXECUTE ON FUNCTION public.privacy_rule_rows() TO bridgeleads_system;" in body
    assert "REVOKE ALL ON FUNCTION public.privacy_rule_rows() FROM PUBLIC, bridgeleads_app;" in body


def test_a_fresh_uuid_rule_id_round_trips_as_text(conn) -> None:
    rid = _request(conn)
    rule = _rule(rid)
    conn.execute(text(_RULE), rule)
    row = conn.execute(text("SELECT id, rule_id FROM public.privacy_rule_rows() "
                            "WHERE rule_id = :r"), {"r": rule["rule"]}).one()
    assert uuid.UUID(row.id) and row.rule_id == rule["rule"]
