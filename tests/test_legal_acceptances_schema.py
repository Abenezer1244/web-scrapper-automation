"""Migration 123: legal_acceptances (append-only Terms / Privacy acceptance record).

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
    email = f"legal_{uid[:8]}@bl.test"
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


_INSERT = ("INSERT INTO legal_acceptances (user_id, document, version, flow) "
           "VALUES (:u, :d, :v, :f)")


def _accept(conn, uid: str, document: str = "terms", version: str = "2026-10-01",
            flow: str = "register") -> None:
    conn.execute(text(_INSERT), {"u": uid, "d": document, "v": version, "f": flow})


def test_a_row_is_stamped_by_the_database_clock(conn) -> None:
    uid = _user(conn)
    _accept(conn, uid)
    row = conn.execute(text(
        "SELECT document, version, flow, accepted_at, accepted_at <= now() AS past "
        "FROM legal_acceptances WHERE user_id = :u"), {"u": uid}).one()
    assert (row.document, row.version, row.flow) == ("terms", "2026-10-01", "register")
    assert row.accepted_at is not None and row.past


def test_one_row_per_user_document_version_but_history_is_kept(conn) -> None:
    uid = _user(conn)
    _accept(conn, uid)
    assert _sqlstate(conn, _INSERT, {"u": uid, "d": "terms", "v": "2026-10-01",
                                     "f": "verify_email"}) == "23505"
    # A new version is a NEW row: re-acceptance never overwrites the earlier one.
    _accept(conn, uid, version="2027-01-15", flow="reaccept")
    _accept(conn, uid, document="privacy", version="2026-10-30")
    assert conn.execute(text("SELECT count(*) FROM legal_acceptances WHERE user_id = :u"),
                        {"u": uid}).scalar() == 3


@pytest.mark.parametrize(("document", "version", "flow"), [
    ("cookies", "2026-10-01", "register"),        # unknown document
    ("terms", "v2", "register"),                   # not a published date
    ("terms", "2026-10-01; drop", "register"),     # free text
    ("terms", "2026-10-01", "admin"),              # unknown flow
])
def test_check_constraints_refuse_free_text(conn, document, version, flow) -> None:
    uid = _user(conn)
    assert _sqlstate(conn, _INSERT, {"u": uid, "d": document, "v": version,
                                     "f": flow}) == "23514"


def test_no_one_may_rewrite_an_acceptance_not_even_the_owner_role(conn) -> None:
    # This connection is the migration owner, which bypasses grants and RLS: the
    # trigger is what makes the record append-only (BLD22, before the 113 fence).
    uid, other = _user(conn), _user(conn)
    _accept(conn, uid)
    rid = conn.execute(text("SELECT id FROM legal_acceptances WHERE user_id = :u"),
                       {"u": uid}).scalar()
    for assignment, params in (
        ("user_id = :o", {"o": other}),
        ("version = '2027-01-01'", {}),
        ("flow = 'reaccept'", {}),
        ("accepted_at = now() - interval '1 year'", {}),
    ):
        assert _sqlstate(conn, f"UPDATE legal_acceptances SET {assignment} WHERE id = :i",
                         {"i": rid, **params}) == "BLD22", assignment


def test_fence_refuses_a_purging_owner(conn) -> None:
    other = _user(conn)
    _bind(conn, other)
    conn.execute(text("SELECT * FROM request_account_deletion()"))
    conn.execute(text("SET LOCAL ROLE bridgeleads_purge"))
    conn.execute(text("UPDATE users SET deletion_state = 'purging' WHERE id = :u"),
                 {"u": other})
    conn.execute(text("RESET ROLE"))
    assert _sqlstate(conn, _INSERT, {"u": other, "d": "terms", "v": "2026-10-01",
                                     "f": "register"}) == "BLD20"


def test_fence_and_immutability_triggers_fire_always_and_rls_is_on(conn) -> None:
    for trigger in ("zz_account_deletion_fence", "legal_acceptances_no_update"):
        assert conn.execute(text(
            "SELECT tgenabled FROM pg_trigger WHERE tgname = :t "
            "AND tgrelid = 'public.legal_acceptances'::regclass"), {"t": trigger}).scalar() == "A"
    assert conn.execute(text(
        "SELECT relrowsecurity FROM pg_class WHERE oid = 'public.legal_acceptances'::regclass"
    )).scalar() is True


def test_supabase_api_roles_get_nothing(conn) -> None:
    present = [r for (r,) in conn.execute(text(
        "SELECT rolname FROM pg_roles WHERE rolname IN "
        "('anon', 'authenticated', 'service_role')")).all()]
    if not present:
        pytest.skip("no Supabase API roles on this cluster")
    for role in present:
        for priv in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert conn.execute(text("SELECT has_table_privilege(:r, 'legal_acceptances', :p)"),
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
                "SELECT has_column_privilege(:r, 'legal_acceptances', :c, :p)"),
                {"r": role, "c": col, "p": p}).scalar()
        return conn.execute(text("SELECT has_table_privilege(:r, 'legal_acceptances', :p)"),
                            {"r": role, "p": p}).scalar()

    # Append-only for everyone: no UPDATE, DELETE or TRUNCATE.
    for role in ("bridgeleads_app", "bridgeleads_system"):
        for p in ("UPDATE", "DELETE", "TRUNCATE"):
            assert not priv(role, p), (role, p)
    # The app writes the four facts; the database stamps id and accepted_at.
    for col in ("user_id", "document", "version", "flow"):
        assert priv("bridgeleads_app", "INSERT", col), col
    for col in ("accepted_at", "id"):
        assert not priv("bridgeleads_app", "INSERT", col), col
    # The worker only reads (account export).
    assert not priv("bridgeleads_system", "INSERT")
    assert priv("bridgeleads_system", "SELECT")

    a, b = _user(conn), _user(conn)
    conn.execute(text("SET LOCAL ROLE bridgeleads_app"))
    _bind(conn, a)
    # RLS WITH CHECK: a session can only record acceptance for itself.
    assert _sqlstate(conn, _INSERT, {"u": b, "d": "terms", "v": "2026-10-01",
                                     "f": "register"}) == "42501"
    _accept(conn, a)
    # A client-chosen time is refused: the app has no INSERT on accepted_at.
    assert _sqlstate(conn, "INSERT INTO legal_acceptances "
                           "(user_id, document, version, flow, accepted_at) "
                           "VALUES (:u, 'privacy', '2026-10-30', 'register', '2001-01-01')",
                     {"u": a}) == "42501"
    assert conn.execute(text("SELECT count(*) FROM legal_acceptances")).scalar() == 1
    _bind(conn, b)
    assert conn.execute(text("SELECT count(*) FROM legal_acceptances")).scalar() == 0
    conn.execute(text("RESET ROLE"))
