"""Migration 125: privacy requests and suppression rules (Phase 6 groundwork, P2).

Real database. Every test runs in one transaction that is rolled back, so nothing
persists. Expected failures run inside a SAVEPOINT so the outer transaction survives.
Role checks SKIP where the roles are absent (CI has no Supabase or runtime roles).

What these tests prove is the database's part only: shapes, append-only, the
activation guard, ordering and the role boundary. That an HMAC came from a valid
subject is src/privacy's guarantee (tests/test_privacy_keys.py, _subjects.py).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.db.session import sync_engine
from src.utils.crypto import encrypt_field

_TABLES = ("privacy_requests", "privacy_request_payloads", "privacy_request_events",
           "privacy_suppressions")
_H1, _H2 = "a" * 64, "b" * 64


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


def _request(conn, kind: str = "delete") -> str:
    return conn.execute(text(
        "INSERT INTO privacy_requests (kind, channel, deadline_at) "
        "VALUES (:k, 'web', now() + interval '45 days') RETURNING id"), {"k": kind}).scalar()


_EVENT = ("INSERT INTO privacy_request_events (request_id, seq, event_type, actor, "
          "actor_user_id, decision_code, deadline_at, verification_level) "
          "VALUES (:r, :seq, :t, :a, :u, :code, :dl, :lvl)")


def _event(**over) -> dict:
    return {"seq": 1, "t": "received", "a": "system", "u": None, "code": None,
            "dl": None, "lvl": None, **over}


_RULE = ("INSERT INTO privacy_suppressions (rule_id, request_id, anchor_kind, anchor_hmac, "
         "subject_kind, subject_hmac, scope, key_version, normalization_version, state) "
         "VALUES (:rule, :r, :ak, :ah, :sk, :sh, :scope, :kv, :nv, :state)")


def _rule(request_id: str, **over) -> dict:
    return {"rule": str(uuid.uuid4()), "r": request_id, "ak": "property", "ah": _H1,
            "sk": "person", "sh": _H2, "scope": "subject", "kv": "v1", "nv": "n3",
            "state": "pending", **over}


# ── The lifecycle the schema is for ─────────────────────────────────────────────

def test_a_request_its_payload_trail_and_a_pending_rule_can_be_recorded(conn) -> None:
    rid = _request(conn)
    conn.execute(text("INSERT INTO privacy_request_payloads (request_id, contact_encrypted) "
                      "VALUES (:r, :c)"), {"r": rid, "c": encrypt_field("jane@example.test")})
    conn.execute(text(_EVENT), {"r": rid, **_event()})
    conn.execute(text(_EVENT), {"r": rid, **_event(seq=2, t="verified", lvl="email_link")})
    conn.execute(text(_EVENT), {"r": rid, **_event(
        seq=3, t="extended", a="operator", u=str(uuid.uuid4()),
        dl="2027-01-01T00:00:00Z")})
    conn.execute(text(_RULE), _rule(rid))
    latest = conn.execute(text(
        "SELECT event_type FROM privacy_request_events WHERE request_id = :r "
        "ORDER BY seq DESC LIMIT 1"), {"r": rid}).scalar()
    assert latest == "extended"  # status is the latest event by seq
    stamped = conn.execute(text(
        "SELECT received_at <= now() FROM privacy_requests WHERE id = :r"), {"r": rid}).scalar()
    assert stamped is True


# ── The activation guard ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("state", ["ACTIVE", "", "suppressed"])
def test_a_rule_is_only_pending_active_or_lifted(conn, state) -> None:
    rid = _request(conn)
    assert _sqlstate(conn, _RULE, _rule(rid, state=state)) == "23514"


def test_136_replaced_the_pending_only_guard(conn) -> None:
    names = set(conn.execute(text(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'public.privacy_suppressions'::regclass")).scalars())
    assert "ck_privacy_suppressions_activation_guard" not in names
    assert {"ck_privacy_suppressions_state", "ck_privacy_suppressions_enforceable"} <= names


@pytest.mark.parametrize("state", ["active", "lifted"])
def test_a_mailing_anchor_never_leaves_pending(conn, state) -> None:
    """The rule loader cannot compute a mailing anchor and would refuse every rule."""
    rid = _request(conn)
    assert _sqlstate(conn, _RULE, _rule(rid, state=state, ak="mailing")) == "23514"


# ── Rule shape ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("over", [
    {"ak": "name"},                      # a rule needs an address or contact anchor
    {"ah": "A" * 64},                    # not HMAC output (uppercase)
    {"ah": "a" * 63},
    {"sk": "household"},
    {"sh": "smith|john"},                # a raw value, not an HMAC
    {"scope": "property_all_persons"},   # an owner decision, not allowed yet
    {"kv": "1"}, {"kv": "v0"}, {"nv": "3"}, {"nv": "n0"},
])
def test_rule_shape_checks(conn, over) -> None:
    rid = _request(conn)
    assert _sqlstate(conn, _RULE, _rule(rid, **over)) == "23514", over


@pytest.mark.parametrize("missing", ["ah", "sh"])
def test_a_rule_without_an_anchor_or_a_subject_cannot_be_stored(conn, missing) -> None:
    rid = _request(conn)
    assert _sqlstate(conn, _RULE, _rule(rid, **{missing: None})) == "23502"


def test_one_rule_has_a_row_per_key_and_normalisation_version(conn) -> None:
    rid = _request(conn)
    rule = str(uuid.uuid4())
    conn.execute(text(_RULE), _rule(rid, rule=rule, kv="v1", nv="n3"))
    conn.execute(text(_RULE), _rule(rid, rule=rule, kv="v2", nv="n3"))
    conn.execute(text(_RULE), _rule(rid, rule=rule, kv="v1", nv="n4"))
    assert _sqlstate(conn, _RULE, _rule(rid, rule=rule, kv="v1", nv="n3")) == "23505"


# ── Request and event shape ─────────────────────────────────────────────────────

@pytest.mark.parametrize(("sql", "params"), [
    ("INSERT INTO privacy_requests (kind, channel, deadline_at) "
     "VALUES ('erase_everything', 'web', now() + interval '1 day')", {}),
    ("INSERT INTO privacy_requests (kind, channel, deadline_at) "
     "VALUES ('delete', 'sms', now() + interval '1 day')", {}),
    ("INSERT INTO privacy_requests (kind, channel, deadline_at) "
     "VALUES ('delete', 'web', now() - interval '1 day')", {}),
    ("INSERT INTO privacy_requests (kind, channel, deadline_at, requester_email_hmac) "
     "VALUES ('delete', 'web', now() + interval '1 day', 'jane@example.test')", {}),
])
def test_request_checks(conn, sql, params) -> None:
    assert _sqlstate(conn, sql, params) == "23514"


@pytest.mark.parametrize("over", [
    {"t": "approved"},                                   # unknown event type
    {"t": "extended"},                                   # extension without its deadline
    {"dl": "2027-01-01T00:00:00Z"},                      # deadline on a non-extension
    {"t": "decided"},                                    # decision without its code
    {"code": "granted"},                                 # code on a non-decision
    {"t": "decided", "code": "Granted!"},                # code shape
    {"lvl": "Email Link"},                               # level shape
    {"a": "operator"},                                   # operator without an id
    {"u": str(uuid.uuid4())},                            # system with an id
    {"a": "robot"},
    {"seq": 0},
])
def test_event_checks(conn, over) -> None:
    rid = _request(conn)
    assert _sqlstate(conn, _EVENT, {"r": rid, **_event(**over)}) == "23514", over


def test_two_writers_cannot_both_take_the_same_seq(conn) -> None:
    rid = _request(conn)
    conn.execute(text(_EVENT), {"r": rid, **_event()})
    assert _sqlstate(conn, _EVENT, {"r": rid, **_event(t="in_review")}) == "23505"


def test_payload_values_must_be_ciphertext(conn) -> None:
    rid = _request(conn)
    for contact, details in (("jane@example.test", None),
                             (encrypt_field("jane@example.test"), "please delete me")):
        assert _sqlstate(conn, "INSERT INTO privacy_request_payloads (request_id, "
                               "contact_encrypted, details_encrypted) VALUES (:r, :c, :d)",
                         {"r": rid, "c": contact, "d": details}) == "23514"


# ── Append-only, erasable payload, rules outlive paperwork ──────────────────────

def test_no_row_in_any_table_can_be_updated_not_even_by_the_owner(conn) -> None:
    rid = _request(conn)
    conn.execute(text("INSERT INTO privacy_request_payloads (request_id, contact_encrypted) "
                      "VALUES (:r, :c)"), {"r": rid, "c": encrypt_field("x@example.test")})
    conn.execute(text(_EVENT), {"r": rid, **_event()})
    conn.execute(text(_RULE), _rule(rid))
    for sql in ("UPDATE privacy_requests SET deadline_at = deadline_at + interval '1 day'",
                "UPDATE privacy_request_payloads SET details_encrypted = contact_encrypted",
                "UPDATE privacy_request_events SET event_type = 'closed'",
                "UPDATE privacy_suppressions SET state = 'pending'"):
        assert _sqlstate(conn, sql) == "BLD24", sql


def test_the_payload_can_be_erased_alone_and_the_trail_survives(conn) -> None:
    rid = _request(conn)
    conn.execute(text("INSERT INTO privacy_request_payloads (request_id, contact_encrypted) "
                      "VALUES (:r, :c)"), {"r": rid, "c": encrypt_field("x@example.test")})
    conn.execute(text(_EVENT), {"r": rid, **_event()})
    conn.execute(text("DELETE FROM privacy_request_payloads WHERE request_id = :r"), {"r": rid})
    assert conn.execute(text("SELECT count(*) FROM privacy_request_events "
                             "WHERE request_id = :r"), {"r": rid}).scalar() == 1


def test_deleting_a_request_never_deletes_its_suppression_rule(conn) -> None:
    rid = _request(conn)
    conn.execute(text(_RULE), _rule(rid))
    assert _sqlstate(conn, "DELETE FROM privacy_requests WHERE id = :r", {"r": rid}) == "23503"


def test_deleting_a_request_with_no_rule_takes_its_payload_and_trail(conn) -> None:
    rid = _request(conn)
    conn.execute(text("INSERT INTO privacy_request_payloads (request_id, contact_encrypted) "
                      "VALUES (:r, :c)"), {"r": rid, "c": encrypt_field("x@example.test")})
    conn.execute(text(_EVENT), {"r": rid, **_event()})
    conn.execute(text("DELETE FROM privacy_requests WHERE id = :r"), {"r": rid})
    for table in ("privacy_request_payloads", "privacy_request_events"):
        assert conn.execute(text(f"SELECT count(*) FROM {table} WHERE request_id = :r"),
                            {"r": rid}).scalar() == 0


def test_actor_ids_are_not_tied_to_users(conn) -> None:
    # No FK: the account purge must never be blocked by, or cascade into, the trail.
    fks = conn.execute(text(
        "SELECT count(*) FROM pg_constraint WHERE contype = 'f' "
        "AND conrelid = 'public.privacy_request_events'::regclass "
        "AND confrelid = 'public.users'::regclass")).scalar()
    assert fks == 0


# ── Lock-down ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("table", _TABLES)
def test_trigger_always_rls_on_no_policies_no_sequences(conn, table) -> None:
    assert conn.execute(text(
        "SELECT tgenabled FROM pg_trigger WHERE tgname = :t "
        "AND tgrelid = ('public.' || :tab)::regclass"),
        {"t": f"{table}_no_update", "tab": table}).scalar() == "A"
    assert conn.execute(text(
        "SELECT relrowsecurity FROM pg_class WHERE oid = ('public.' || :tab)::regclass"),
        {"tab": table}).scalar() is True
    assert conn.execute(text("SELECT count(*) FROM pg_policies WHERE tablename = :tab"),
                        {"tab": table}).scalar() == 0
    assert conn.execute(text(
        "SELECT count(*) FROM pg_depend d JOIN pg_class s ON s.oid = d.objid "
        "WHERE s.relkind = 'S' AND d.refobjid = ('public.' || :tab)::regclass"),
        {"tab": table}).scalar() == 0


def _probe_role(conn) -> str:
    # A throwaway NOSUPERUSER NOBYPASSRLS role: the owner bypasses RLS and would
    # make these tests pass falsely. CREATE ROLE is transactional; the rollback drops it.
    role = f"privacy_probe_{uuid.uuid4().hex[:8]}"
    conn.execute(text(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS"))
    conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
    return role


def test_without_grants_an_ordinary_role_is_refused(conn) -> None:
    role = _probe_role(conn)
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    for table in _TABLES:
        assert _sqlstate(conn, f"SELECT 1 FROM {table} LIMIT 1") == "42501", table
    conn.execute(text("RESET ROLE"))


def test_with_grants_rls_and_no_policy_still_hide_and_refuse_everything(conn) -> None:
    # Codex: the test above proves the ACL, not RLS. Here the role HOLDS every
    # privilege, so only RLS with zero policies stands between it and the rows.
    rid = _request(conn)
    conn.execute(text("INSERT INTO privacy_request_payloads (request_id, contact_encrypted) "
                      "VALUES (:r, :c)"), {"r": rid, "c": encrypt_field("x@example.test")})
    conn.execute(text(_EVENT), {"r": rid, **_event()})
    conn.execute(text(_RULE), _rule(rid))
    role = _probe_role(conn)
    for table in _TABLES:
        conn.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}"))
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    for table in _TABLES:
        assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0, table
        # UPDATE and DELETE see no row through RLS, so they touch nothing.
        assert conn.execute(text(f"DELETE FROM {table}")).rowcount == 0, table
    # INSERT is refused by RLS itself (no policy admits the new row).
    assert _sqlstate(conn, "INSERT INTO privacy_requests (kind, channel, deadline_at) "
                           "VALUES ('delete', 'web', now() + interval '1 day')") == "42501"
    conn.execute(text("RESET ROLE"))
    assert conn.execute(text("SELECT count(*) FROM privacy_suppressions "
                             "WHERE request_id = :r"), {"r": rid}).scalar() == 1


_SCRIPTS = ("scripts/provision_rls_roles.sql", "scripts/_cutover_step2_grants_policies.py")
_REVOKED_ROLES = ("anon", "authenticated", "service_role", "bridgeleads_app",
                  "bridgeleads_system", "bridgeleads_purge")


@pytest.mark.parametrize("script", _SCRIPTS)
def test_both_grant_scripts_revoke_what_the_migration_revokes(script) -> None:
    # The scripts re-run blanket grants after the migration, so each must take
    # every privacy table back from every role migration 125 does (Codex).
    from pathlib import Path
    body = Path(__file__).resolve().parent.parent.joinpath(script).read_text(encoding="utf-8")
    start = body.index("IF to_regclass('public.privacy_requests') IS NOT NULL THEN")
    block = body[start:body.index("\n    END IF;", start) if script.endswith(".sql")
                 else body.index("\n        END IF;\n    END", start)]
    for table in _TABLES:
        assert table in block, (script, table)
    for role in _REVOKED_ROLES:
        assert f"FROM {role}" in block or f", {role}" in block, (script, role)


@pytest.mark.parametrize("roles", [("anon", "authenticated", "service_role"),
                                   ("bridgeleads_app", "bridgeleads_system")])
def test_api_and_runtime_roles_hold_nothing(conn, roles) -> None:
    present = [r for (r,) in conn.execute(text(
        "SELECT rolname FROM pg_roles WHERE rolname = ANY(:rs)"), {"rs": list(roles)}).all()]
    if not present:
        pytest.skip(f"none of {roles} on this cluster")
    for role in present:
        for table in _TABLES:
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES"):
                assert conn.execute(text("SELECT has_table_privilege(:r, :t, :p)"),
                                    {"r": role, "t": f"public.{table}", "p": priv}
                                    ).scalar() is False, (role, table, priv)
