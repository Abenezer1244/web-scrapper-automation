"""Migration 115: account_exports (the data-export request row).

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

from src.api.auth import hash_password
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


def _user(conn) -> str:
    uid = str(uuid.uuid4())
    email = f"exp_{uid[:8]}@bl.test"
    conn.execute(
        text("""
            INSERT INTO users (id, email, email_hmac, password_hash, plan, records_used,
                records_limit, is_active, is_admin, referral_credit_cents)
            VALUES (:id, :e, :h, :pw, 'starter', 0, 50, true, false, 0)
        """),
        {"id": uid, "e": email, "h": blind_index(email), "pw": hash_password("Pw-123456789")},
    )
    return uid


def _bind(conn, uid: str) -> None:
    conn.execute(text("SELECT set_config('app.current_user_id', :u, true)"), {"u": uid})


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


def _export(conn, uid: str) -> str:
    return conn.execute(text("INSERT INTO account_exports (user_id) VALUES (:u) RETURNING id"),
                        {"u": uid}).scalar()


def _set_deletion_state(conn, uid: str, state: str) -> None:
    """Move the owner's lifecycle as the purge role (what the 112/113 functions do)."""
    _bind(conn, uid)
    conn.execute(text("SELECT * FROM request_account_deletion()"))
    if state == "purging":
        conn.execute(text("SET LOCAL ROLE bridgeleads_purge"))
        conn.execute(text("UPDATE users SET deletion_state = 'purging' WHERE id = :u"),
                     {"u": uid})
        conn.execute(text("RESET ROLE"))


def test_a_new_row_is_a_pending_request_and_one_may_be_open_per_account(conn) -> None:
    uid = _user(conn)
    eid = _export(conn, uid)
    row = conn.execute(text("SELECT status, attempts, email_attempts, requested_at "
                            "FROM account_exports WHERE id = :i"), {"i": eid}).one()
    assert (row.status, row.attempts, row.email_attempts) == ("pending", 0, 0)
    assert row.requested_at is not None
    assert _sqlstate(conn, "INSERT INTO account_exports (user_id) VALUES (:u)",
                     {"u": uid}) == "23505"
    # Once it is finished, the next one may open (the 24 h limit is the route's).
    conn.execute(text("UPDATE account_exports SET status = 'failed' WHERE id = :i"), {"i": eid})
    _export(conn, uid)


def test_status_invariants(conn) -> None:
    eid = _export(conn, _user(conn))
    p = {"i": eid}
    assert _sqlstate(conn, "UPDATE account_exports SET status = 'done' WHERE id = :i", p) == "23514"
    # ready needs its size and both timestamps.
    assert _sqlstate(conn, "UPDATE account_exports SET status = 'ready' WHERE id = :i", p) == "23514"
    # The link lasts exactly 7 days from ready.
    assert _sqlstate(conn, "UPDATE account_exports SET status = 'ready', size_bytes = 1, "
                           "ready_at = now(), expires_at = now() + interval '8 days' "
                           "WHERE id = :i", p) == "23514"
    conn.execute(text("UPDATE account_exports SET status = 'ready', size_bytes = 1, "
                      "ready_at = now(), expires_at = now() + interval '7 days' WHERE id = :i"), p)


def test_fence_refuses_a_purging_owner_and_pins_the_owner(conn) -> None:
    uid, other = _user(conn), _user(conn)
    eid = _export(conn, uid)
    assert _sqlstate(conn, "UPDATE account_exports SET user_id = :o WHERE id = :i",
                     {"o": other, "i": eid}) == "BLD21"
    conn.execute(text("UPDATE account_exports SET status = 'failed' WHERE id = :i"), {"i": eid})
    # pending: still allowed (the route's 403 gate refuses it; the DB fences purging only).
    _set_deletion_state(conn, uid, "pending")
    pending_export = _export(conn, uid)
    conn.execute(text("UPDATE account_exports SET status = 'failed' WHERE id = :i"),
                 {"i": pending_export})
    _set_deletion_state(conn, other, "purging")
    assert _sqlstate(conn, "INSERT INTO account_exports (user_id) VALUES (:u)",
                     {"u": other}) == "BLD20"


def test_fence_trigger_fires_always(conn) -> None:
    assert conn.execute(text(
        "SELECT tgenabled FROM pg_trigger WHERE tgname = 'zz_account_deletion_fence' "
        "AND tgrelid = 'public.account_exports'::regclass")).scalar() == "A"


def test_supabase_api_roles_get_nothing(conn) -> None:
    present = [r for (r,) in conn.execute(text(
        "SELECT rolname FROM pg_roles WHERE rolname IN "
        "('anon', 'authenticated', 'service_role')")).all()]
    if not present:
        pytest.skip("no Supabase API roles on this cluster")
    for role in present:
        for priv in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert conn.execute(text("SELECT has_table_privilege(:r, 'account_exports', :p)"),
                                {"r": role, "p": priv}).scalar() is False, (role, priv)


@pytest.mark.integration
def test_runtime_roles(conn) -> None:
    n = conn.execute(text("SELECT count(*) FROM pg_roles WHERE rolname IN "
                          "('bridgeleads_app', 'bridgeleads_system')")).scalar()
    if n != 2:
        pytest.skip("bridgeleads_app/system not provisioned")
    current = conn.execute(text("SELECT current_user")).scalar()
    sp = conn.begin_nested()
    try:
        for role in ("bridgeleads_app", "bridgeleads_system"):
            conn.execute(text(f'GRANT {role} TO "{current}"'))
        sp.commit()
    except DBAPIError:
        sp.rollback()
        pytest.skip(f"{current!r} cannot GRANT the runtime roles (needs an owner DSN)")

    def priv(role: str, p: str, col: str | None = None) -> bool:
        if col:
            return conn.execute(text(
                "SELECT has_column_privilege(:r, 'account_exports', :c, :p)"),
                {"r": role, "c": col, "p": p}).scalar()
        return conn.execute(text("SELECT has_table_privilege(:r, 'account_exports', :p)"),
                            {"r": role, "p": p}).scalar()

    # Nobody deletes: the rows are the 24-month request log.
    for role in ("bridgeleads_app", "bridgeleads_system"):
        assert not priv(role, "DELETE") and not priv(role, "TRUNCATE"), role
    # The app inserts a request (user_id only) and reads; it never sets system fields.
    assert priv("bridgeleads_app", "INSERT", "user_id")
    for col in ("status", "size_bytes", "ready_at", "expires_at", "claim_id", "id"):
        assert not priv("bridgeleads_app", "INSERT", col), col
    assert not priv("bridgeleads_app", "UPDATE")
    # The worker moves a row but never inserts one or re-parents it.
    assert not priv("bridgeleads_system", "INSERT")
    for col in ("user_id", "id", "requested_at"):
        assert not priv("bridgeleads_system", "UPDATE", col), col
    assert priv("bridgeleads_system", "UPDATE", "status")

    a, b = _user(conn), _user(conn)
    conn.execute(text("SET LOCAL ROLE bridgeleads_app"))
    _bind(conn, a)
    # RLS WITH CHECK: a session can only queue an export for itself.
    assert _sqlstate(conn, "INSERT INTO account_exports (user_id) VALUES (:u)",
                     {"u": b}) == "42501"
    conn.execute(text("INSERT INTO account_exports (user_id) VALUES (:u)"), {"u": a})
    assert conn.execute(text("SELECT count(*) FROM account_exports")).scalar() == 1
    _bind(conn, b)
    assert conn.execute(text("SELECT count(*) FROM account_exports")).scalar() == 0
    conn.execute(text("RESET ROLE"))

    conn.execute(text("SET LOCAL ROLE bridgeleads_system"))
    assert conn.execute(text(
        "UPDATE account_exports SET status = 'building' WHERE user_id = :u"),
        {"u": a}).rowcount == 1
    conn.execute(text("RESET ROLE"))
