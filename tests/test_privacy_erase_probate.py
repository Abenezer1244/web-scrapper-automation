"""Migration 160: privacy_erase_probate() has privacy_erase_batch's security contract.

Real database, one transaction rolled back at the end (the helpers and fixture are
test_privacy_erase_batch's). Synthetic people only.
"""
from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.test_privacy_erase_batch import _id, _rule, conn  # noqa: F401 -- fixture

_CALL = "SELECT privacy_erase_probate(CAST(:rule AS uuid), CAST(:ids AS uuid[]))"


def _sqlstate(conn, rule: str, ids: list[str]) -> str | None:  # noqa: F811
    sp = conn.begin_nested()
    try:
        conn.execute(text(_CALL), {"rule": rule, "ids": ids})
    except DBAPIError as exc:
        sp.rollback()
        return getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    sp.rollback()
    return None


def _notice(conn) -> str:  # noqa: F811
    return str(conn.execute(text(
        "INSERT INTO probate_notices (id, county, state, source, cause_no, first_published, "
        "decedent, personal_reps, claims_address, parcels) VALUES (gen_random_uuid(), "
        "'asotin', 'WA', 'test', :c, current_date, 'JOHN SMITH', CAST('[\"MARY SMITH\"]' "
        "AS json), 'PO BOX 1', CAST('[]' AS json)) RETURNING id"),
        {"c": uuid.uuid4().hex[:12]}).scalar())


def test_erases_the_named_notices_once(conn):  # noqa: F811
    rule = _rule(conn, "delete", ("pending", "active"))
    hit, miss = _notice(conn), _notice(conn)
    assert conn.execute(text(_CALL), {"rule": rule, "ids": [hit]}).scalar() == {"probate": 1}
    assert conn.execute(text(_CALL), {"rule": rule, "ids": [hit]}).scalar() == {"probate": 0}
    rows = dict(conn.execute(text(
        "SELECT id::text, decedent FROM probate_notices WHERE id = ANY(CAST(:i AS uuid[]))"),
        {"i": [hit, miss]}).all())
    assert rows == {hit: None, miss: "JOHN SMITH"}


@pytest.mark.parametrize("kind,states", [
    ("delete", ("pending",)),
    ("opt_out", ("pending", "active")),
    ("delete", ("pending", "active", "lifted")),
])
def test_refuses_anything_but_an_active_deletion_rule(conn, kind, states):  # noqa: F811
    assert _sqlstate(conn, _rule(conn, kind, states), []) == "BLD41"


def test_refuses_an_oversized_batch(conn):  # noqa: F811
    rule = _rule(conn, "delete", ("pending", "active"))
    assert _sqlstate(conn, rule, [_id() for _ in range(501)]) == "BLD40"


def test_owned_by_purge_definer_pinned_path_system_only(conn):  # noqa: F811
    owner, secdef, config, acl = conn.execute(text(
        "SELECT pg_get_userbyid(p.proowner), p.prosecdef, p.proconfig, "
        "coalesce(p.proacl::text, '') FROM pg_proc p WHERE p.proname = 'privacy_erase_probate'"
    )).one()
    assert owner == "bridgeleads_purge" and secdef is True
    assert config == ["search_path=pg_catalog, pg_temp"]
    assert not re.search(r"(^|[{,])=X/", acl)  # no PUBLIC execute
    fn = "public.privacy_erase_probate(uuid, uuid[])"
    for role in ("bridgeleads_app", "anon", "authenticated", "service_role"):
        if conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first():
            assert conn.execute(text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                                {"r": role, "f": fn}).scalar() is False
    if conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system'")).first():
        assert conn.execute(text("SELECT has_function_privilege('bridgeleads_system', :f, "
                                 "'EXECUTE')"), {"f": fn}).scalar() is True


def test_a_role_without_execute_is_refused(conn):  # noqa: F811
    rule = _rule(conn, "delete", ("pending", "active"))
    role = f"privacy_probe_{uuid.uuid4().hex[:8]}"
    conn.execute(text(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS"))
    conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    try:
        assert _sqlstate(conn, rule, []) == "42501"
    finally:
        conn.execute(text("RESET ROLE"))
