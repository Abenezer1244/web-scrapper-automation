"""Migration 112: account-deletion lifecycle (request/restore functions + guards).

Real database. Every test runs in one transaction that is rolled back, so nothing
persists. Expected failures run inside a SAVEPOINT so the outer transaction survives.

The role-boundary tests at the bottom need the provisioned bridgeleads_app/system roles
and SKIP where they are absent (CI); everything above runs everywhere, because the
migration itself creates bridgeleads_purge.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

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


def _user(conn, active: bool = True) -> str:
    uid = str(uuid.uuid4())
    email = f"del_{uid[:8]}@bl.test"
    conn.execute(
        text("""
            INSERT INTO users (id, email, email_hmac, password_hash, plan, records_used,
                records_limit, is_active, is_admin, referral_credit_cents)
            VALUES (:id, :e, :h, :pw, 'starter', 0, 50, :active, false, 0)
        """),
        {"id": uid, "e": email, "h": blind_index(email), "pw": hash_password("Pw-123456789"),
         "active": active},
    )
    return uid


def _bind(conn, uid: str | None) -> None:
    conn.execute(text("SELECT set_config('app.current_user_id', :u, true)"), {"u": uid or ""})


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


def _state(conn, uid: str):
    return conn.execute(text("SELECT deletion_state FROM users WHERE id = :u"), {"u": uid}).scalar()


def _rows(conn, uid: str):
    return conn.execute(
        text("SELECT id, status, purge_after, stripe_state FROM account_deletions "
             "WHERE user_id = :u ORDER BY requested_at"),
        {"u": uid},
    ).all()


def _to_purging(conn, uid: str) -> None:
    """What the P3 claim function will do, done here as the purge role."""
    conn.execute(text("SET LOCAL ROLE bridgeleads_purge"))
    # The plan's lock order: users row first, then the account_deletions row.
    conn.execute(text("UPDATE users SET deletion_state = 'purging' WHERE id = :u"), {"u": uid})
    conn.execute(text("UPDATE account_deletions SET status = 'purging' "
                      "WHERE user_id = :u AND status = 'pending'"), {"u": uid})
    conn.execute(text("RESET ROLE"))


def test_request_opens_one_pending_deletion_30_days_out(conn) -> None:
    uid = _user(conn)
    _bind(conn, uid)
    before = datetime.now(UTC)
    first = conn.execute(text("SELECT * FROM request_account_deletion()")).one()
    assert first.created is True
    # +-2 h: `interval '30 days'` follows the session time zone across a DST change.
    assert timedelta(days=29, hours=22) < first.purge_after - before < timedelta(days=30, hours=2)
    assert _state(conn, uid) == "pending"
    rows = _rows(conn, uid)
    assert [(r.status, r.stripe_state) for r in rows] == [("pending", "pending_cancel")]

    # A repeat request is the same request: same id, deadline NOT pushed back.
    again = conn.execute(text("SELECT * FROM request_account_deletion()")).one()
    assert (again.deletion_id, again.purge_after, again.created) == (
        first.deletion_id, first.purge_after, False)
    assert len(_rows(conn, uid)) == 1


def test_restore_reopens_the_account_and_owes_a_stripe_uncancel(conn) -> None:
    uid = _user(conn)
    _bind(conn, uid)
    deletion_id = conn.execute(text("SELECT deletion_id FROM request_account_deletion()")).scalar()
    assert conn.execute(text("SELECT restore_account_deletion()")).scalar() == deletion_id
    assert _state(conn, uid) is None
    assert [(r.status, r.stripe_state) for r in _rows(conn, uid)] == [
        ("restored", "pending_uncancel")]
    assert _sqlstate(conn, "SELECT restore_account_deletion()") == "BLD02"
    # Restored, so a new request opens a NEW operation.
    again = conn.execute(text("SELECT * FROM request_account_deletion()")).one()
    assert again.created is True and again.deletion_id != deletion_id


def test_functions_act_only_on_the_bound_user(conn) -> None:
    a, b = _user(conn), _user(conn)
    _bind(conn, None)
    assert _sqlstate(conn, "SELECT * FROM request_account_deletion()") == "BLD04"
    _bind(conn, a)
    conn.execute(text("SELECT * FROM request_account_deletion()"))
    _bind(conn, b)
    assert _sqlstate(conn, "SELECT restore_account_deletion()") == "BLD02"
    assert (_state(conn, a), _state(conn, b)) == ("pending", None)


def test_inactive_or_deleted_accounts_cannot_request(conn) -> None:
    uid = _user(conn, active=False)
    _bind(conn, uid)
    assert _sqlstate(conn, "SELECT * FROM request_account_deletion()") == "BLD03"


def test_once_purging_neither_request_nor_restore_can_touch_it(conn) -> None:
    uid = _user(conn)
    _bind(conn, uid)
    conn.execute(text("SELECT * FROM request_account_deletion()"))
    _to_purging(conn, uid)
    _bind(conn, uid)
    assert _sqlstate(conn, "SELECT restore_account_deletion()") == "BLD01"
    assert _sqlstate(conn, "SELECT * FROM request_account_deletion()") == "BLD01"
    assert _state(conn, uid) == "purging"


def test_no_one_but_the_functions_moves_the_lifecycle(conn) -> None:
    """The owner/superuser running these tests is the strongest non-purge writer."""
    uid = _user(conn)
    assert _sqlstate(conn, "UPDATE users SET deletion_state = 'pending' WHERE id = :u",
                     {"u": uid}) == "BLD10"
    assert _sqlstate(
        conn,
        "INSERT INTO account_deletions (user_id, purge_after) VALUES (:u, now())",
        {"u": uid},
    ) == "BLD10"
    _bind(conn, uid)
    conn.execute(text("SELECT * FROM request_account_deletion()"))
    assert _sqlstate(conn, "UPDATE users SET deletion_state = NULL WHERE id = :u",
                     {"u": uid}) == "BLD10"
    assert _sqlstate(conn, "UPDATE account_deletions SET status = 'restored' "
                           "WHERE user_id = :u", {"u": uid}) == "BLD10"
    # Even the purge role only gets the legal transitions.
    conn.execute(text("SET LOCAL ROLE bridgeleads_purge"))
    assert _sqlstate(conn, "UPDATE users SET deletion_state = 'deleted' WHERE id = :u",
                     {"u": uid}) == "BLD11"
    assert _sqlstate(conn, "UPDATE account_deletions SET status = 'completed' "
                           "WHERE user_id = :u", {"u": uid}) == "BLD11"
    for change in ("purge_after = now() + interval '90 days'",
                   "requested_at = now() - interval '1 day'",
                   "id = gen_random_uuid()"):
        assert _sqlstate(conn, f"UPDATE account_deletions SET {change} WHERE user_id = :u",
                         {"u": uid}) == "BLD11", change
    conn.execute(text("RESET ROLE"))
    # Ordinary writes to other users columns are untouched by the guard.
    conn.execute(text("UPDATE users SET timezone = 'UTC' WHERE id = :u"), {"u": uid})


def test_purge_role_and_functions_are_locked_down(conn) -> None:
    role = conn.execute(text(
        "SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, "
        "rolinherit, rolreplication FROM pg_roles WHERE rolname = 'bridgeleads_purge'"
    )).one()
    assert not any(role)
    assert conn.execute(text(
        "SELECT has_schema_privilege('bridgeleads_purge', 'public', 'CREATE')")).scalar() is False
    # The ownership hand-over's temporary SET grant was taken back: no role can become
    # or inherit the purge role (an ADMIN-only row from CREATE ROLE is inert).
    assert conn.execute(text(
        "SELECT count(*) FROM pg_auth_members WHERE roleid = 'bridgeleads_purge'::regrole "
        "AND (set_option OR inherit_option OR member <> current_user::regrole)")).scalar() == 0
    for fn in ("request_account_deletion", "restore_account_deletion"):
        owner, definer, config, acl = conn.execute(text(
            "SELECT pg_get_userbyid(proowner), prosecdef, proconfig, proacl::text "
            "FROM pg_proc WHERE proname = :f"), {"f": fn}).one()
        assert (owner, definer) == ("bridgeleads_purge", True)
        assert config == ["search_path=pg_catalog, pg_temp"]
        # No PUBLIC entry ("=X/...") in the ACL.
        assert acl is not None and not any(e.startswith("=") for e in acl.strip("{}").split(","))
    for guard in ("users_deletion_state_guard", "account_deletions_guard"):
        assert conn.execute(text("SELECT prosecdef FROM pg_proc WHERE proname = :f"),
                            {"f": guard}).scalar() is False
    # ENABLE ALWAYS ('A'): the guards fire even under session_replication_role=replica.
    enabled = dict(conn.execute(text(
        "SELECT tgname, tgenabled FROM pg_trigger WHERE tgname IN "
        "('users_deletion_state_guard_ins', 'users_deletion_state_guard_upd', "
        "'account_deletions_guard')")).all())
    assert set(enabled.values()) == {"A"} and len(enabled) == 3


# ── Role boundaries: need the provisioned runtime roles (skip in CI) ─────────────

def _require_runtime_roles(conn) -> None:
    n = conn.execute(text("SELECT count(*) FROM pg_roles WHERE rolname IN "
                          "('bridgeleads_app', 'bridgeleads_system')")).scalar()
    if n != 2:
        pytest.skip("bridgeleads_app/system not provisioned")
    current = conn.execute(text("SELECT current_user")).scalar()
    try:
        sp = conn.begin_nested()
        for role in ("bridgeleads_app", "bridgeleads_system"):
            conn.execute(text(f'GRANT {role} TO "{current}"'))
        sp.commit()
    except DBAPIError:
        sp.rollback()
        pytest.skip(f"{current!r} cannot GRANT the runtime roles (needs an owner DSN)")


@pytest.mark.integration
def test_runtime_roles_reach_the_lifecycle_only_through_the_functions(conn) -> None:
    _require_runtime_roles(conn)
    uid = _user(conn)

    # SET ROLE / SET SESSION AUTHORIZATION are checked against the SESSION user (the
    # superuser running this test), so probe the membership graph directly: neither
    # runtime role may hold any path to the purge role.
    for role in ("bridgeleads_app", "bridgeleads_system"):
        for priv in ("MEMBER", "USAGE", "SET"):
            assert conn.execute(text(
                "SELECT pg_has_role(:r, 'bridgeleads_purge', :p)"),
                {"r": role, "p": priv}).scalar() is False, (role, priv)
        # users CASCADEs into account_deletions: deleting a user would be a second,
        # unguarded way to erase a deletion's record. Neither role may do it.
        for priv in ("DELETE", "TRUNCATE"):
            assert conn.execute(text("SELECT has_table_privilege(:r, 'users', :p)"),
                                {"r": role, "p": priv}).scalar() is False, (role, priv)

    conn.execute(text("SET LOCAL ROLE bridgeleads_system"))
    assert _sqlstate(conn, "SELECT * FROM request_account_deletion()") == "42501"
    assert _sqlstate(conn, "SELECT 1 FROM account_deletions") == "42501"
    assert _sqlstate(conn, "UPDATE users SET deletion_state = 'pending' WHERE id = :u",
                     {"u": uid}) == "BLD10"
    conn.execute(text("RESET ROLE"))

    conn.execute(text("SET LOCAL ROLE bridgeleads_app"))
    _bind(conn, uid)
    assert conn.execute(text("SELECT count(*) FROM users WHERE id = :u"), {"u": uid}).scalar() == 1
    assert _sqlstate(conn, "INSERT INTO account_deletions (user_id, purge_after) "
                           "VALUES (:u, now())", {"u": uid}) == "42501"
    assert _sqlstate(conn, "UPDATE users SET deletion_state = 'pending' WHERE id = :u",
                     {"u": uid}) == "BLD10"
    _bind(conn, uid)
    assert conn.execute(text("SELECT created FROM request_account_deletion()")).scalar() is True
    # The app sees its own deletion row through the GUC policy, and only that.
    assert conn.execute(text("SELECT count(*) FROM account_deletions")).scalar() == 1
    _bind(conn, str(uuid.uuid4()))
    assert conn.execute(text("SELECT count(*) FROM account_deletions")).scalar() == 0
    _bind(conn, uid)
    assert conn.execute(text("SELECT restore_account_deletion()")).scalar() is not None
    conn.execute(text("RESET ROLE"))
    assert _state(conn, uid) is None


def test_supabase_api_roles_get_nothing(conn) -> None:
    """Supabase default privileges hand anon/authenticated/service_role ALL on new
    tables and EXECUTE on new functions; 112 revokes them. Skips per absent role."""
    present = [r for (r,) in conn.execute(text(
        "SELECT rolname FROM pg_roles WHERE rolname IN "
        "('anon', 'authenticated', 'service_role')")).all()]
    if not present:
        pytest.skip("no Supabase API roles on this cluster")
    for role in present:
        for tbl in ("account_deletions", "consumed_trial_emails"):
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert conn.execute(text("SELECT has_table_privilege(:r, :t, :p)"),
                                    {"r": role, "t": tbl, "p": priv}).scalar() is False
        for fn in ("request_account_deletion()", "restore_account_deletion()"):
            assert conn.execute(text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"),
                                {"r": role, "f": fn}).scalar() is False
        # users CASCADEs into account_deletions.
        for priv in ("DELETE", "TRUNCATE"):
            assert conn.execute(text("SELECT has_table_privilege(:r, 'users', :p)"),
                                {"r": role, "p": priv}).scalar() is False, (role, priv)
