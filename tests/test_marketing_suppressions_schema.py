"""Migration 124: marketing_email_suppressions (append-only commercial-email opt-outs).

Real database. Every test runs in one transaction that is rolled back, so nothing
persists. Expected failures run inside a SAVEPOINT so the outer transaction survives.
The role-boundary test needs the provisioned bridgeleads_app/system roles and SKIPS
where they are absent (CI).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.db.session import sync_engine
from src.utils.crypto import blind_index


@pytest.fixture
def conn():
    with sync_engine.connect() as c:
        trans = c.begin()
        try:
            yield c
        finally:
            trans.rollback()


def _sqlstate(conn, sql: str, params: dict | None = None) -> str | None:
    """Run `sql` in a savepoint; return the SQLSTATE it raised, or None."""
    sp = conn.begin_nested()
    try:
        conn.execute(text(sql), params or {})
    except DBAPIError as exc:
        sp.rollback()
        return getattr(exc.orig, "pgcode", None)
    sp.rollback()
    return None


_INSERT = ("INSERT INTO marketing_email_suppressions (email_hmac, source) "
           "VALUES (:h, :s)")


def _hmac() -> str:
    return blind_index(f"suppress_{uuid.uuid4().hex[:8]}@bl.test")


def test_an_opt_out_is_stamped_by_the_database_clock(conn) -> None:
    h = _hmac()
    conn.execute(text(_INSERT), {"h": h, "s": "one_click"})
    row = conn.execute(text(
        "SELECT source, opted_out_at <= now() AS past FROM marketing_email_suppressions "
        "WHERE email_hmac = :h"), {"h": h}).one()
    assert row.source == "one_click" and row.past


def test_one_row_per_address(conn) -> None:
    h = _hmac()
    conn.execute(text(_INSERT), {"h": h, "s": "link"})
    assert _sqlstate(conn, _INSERT, {"h": h, "s": "one_click"}) == "23505"
    # The writer's idempotent form keeps the FIRST opt-out.
    conn.execute(text(_INSERT + " ON CONFLICT (email_hmac) DO NOTHING"),
                 {"h": h, "s": "one_click"})
    assert conn.execute(text("SELECT source FROM marketing_email_suppressions "
                             "WHERE email_hmac = :h"), {"h": h}).scalar() == "link"


@pytest.mark.parametrize(("email_hmac", "source"), [
    ("jane@example.com", "link"),            # a raw address is refused
    ("ABCDEF" + "0" * 58, "link"),           # uppercase: not blind_index output
    ("0" * 63, "link"),                      # wrong length
    ("0" * 64, "admin"),                     # unknown source
])
def test_check_constraints(conn, email_hmac, source) -> None:
    assert _sqlstate(conn, _INSERT, {"h": email_hmac, "s": source}) == "23514"


def test_no_one_may_rewrite_an_opt_out_not_even_the_owner_role(conn) -> None:
    h = _hmac()
    conn.execute(text(_INSERT), {"h": h, "s": "link"})
    for assignment in ("source = 'support'", "opted_out_at = now() - interval '1 year'",
                       f"email_hmac = '{'1' * 64}'"):
        assert _sqlstate(conn, f"UPDATE marketing_email_suppressions SET {assignment} "
                               "WHERE email_hmac = :h", {"h": h}) == "BLD23", assignment


def test_trigger_fires_always_and_rls_is_on(conn) -> None:
    assert conn.execute(text(
        "SELECT tgenabled FROM pg_trigger WHERE tgname = 'marketing_email_suppressions_no_update' "
        "AND tgrelid = 'public.marketing_email_suppressions'::regclass")).scalar() == "A"
    assert conn.execute(text(
        "SELECT relrowsecurity FROM pg_class "
        "WHERE oid = 'public.marketing_email_suppressions'::regclass")).scalar() is True


def test_supabase_api_roles_get_nothing(conn) -> None:
    present = [r for (r,) in conn.execute(text(
        "SELECT rolname FROM pg_roles WHERE rolname IN "
        "('anon', 'authenticated', 'service_role')")).all()]
    if not present:
        pytest.skip("no Supabase API roles on this cluster")
    for role in present:
        for priv in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert conn.execute(text(
                "SELECT has_table_privilege(:r, 'marketing_email_suppressions', :p)"),
                {"r": role, "p": priv}).scalar() is False, (role, priv)


@pytest.mark.integration
def test_runtime_roles(conn) -> None:
    n = conn.execute(text("SELECT count(*) FROM pg_roles WHERE rolname IN "
                          "('bridgeleads_app', 'bridgeleads_system')")).scalar()
    if n != 2:
        pytest.skip("bridgeleads_app/system not provisioned")

    def priv(role: str, p: str, col: str | None = None) -> bool:
        if col:
            return conn.execute(text(
                "SELECT has_column_privilege(:r, 'marketing_email_suppressions', :c, :p)"),
                {"r": role, "c": col, "p": p}).scalar()
        return conn.execute(text(
            "SELECT has_table_privilege(:r, 'marketing_email_suppressions', :p)"),
            {"r": role, "p": p}).scalar()

    for role in ("bridgeleads_app", "bridgeleads_system"):
        for p in ("UPDATE", "DELETE", "TRUNCATE"):
            assert not priv(role, p), (role, p)
    assert priv("bridgeleads_app", "INSERT", "email_hmac")
    assert priv("bridgeleads_app", "INSERT", "source")
    assert not priv("bridgeleads_app", "INSERT", "opted_out_at")
    assert not priv("bridgeleads_system", "INSERT")
    assert priv("bridgeleads_system", "SELECT")
