"""Migration 139: the DROP ledger and the definer functions the worker records through.

Real database, one rolled-back transaction per test. Synthetic DROP Ids and hashes only.
"""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.db.session import sync_engine

_FUNCTIONS = (
    "drop_begin_download(text, jsonb, jsonb, jsonb)",
    "drop_add_identifiers(uuid, text, text[], text[])",
    "drop_mark_removed(text, text[], text[])",
    "drop_last_pull()",
    "drop_pending_hashes(uuid, text, uuid, integer)",
)


def _h(seed: str) -> str:
    return base64.b64encode(hashlib.sha256(seed.encode()).digest()).decode()


def _id(n: int) -> str:
    return f"LedgerTst{n:03d}"


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


def _begin(conn, name: str | None, counts: dict | None = None) -> str:
    return str(_q(conn, "SELECT drop_begin_download(:f, CAST(:c AS jsonb), '{}', "
                        "CAST(:lf AS jsonb))",
                  f=name, c=json.dumps(counts or {}),
                  lf=json.dumps({"email": "20260312_4821_Email.csv"} if name else {})).scalar())


def _add(conn, did: str, list_type: str, pairs: list[tuple[str, str]]) -> int:
    return _q(conn, "SELECT drop_add_identifiers(CAST(:d AS uuid), :t, CAST(:i AS text[]), "
                    "CAST(:h AS text[]))", d=did, t=list_type, i=[p[0] for p in pairs],
              h=[p[1] for p in pairs]).scalar()


def _zip_name() -> str:
    return f"20260312_{uuid.uuid4().hex[:8]}_DROP.zip"


def _pgcode(conn, sql: str, **params) -> str | None:
    sp = conn.begin_nested()
    with pytest.raises(DBAPIError) as err:
        conn.execute(text(sql), params)
    sp.rollback()
    return getattr(err.value.orig, "pgcode", None)


def test_a_download_and_its_identifiers_are_recorded_with_the_dated_due_rule(conn):
    did = _begin(conn, _zip_name(), {"email": 2, "phone": 1})
    assert _add(conn, did, "email", [(_id(1), _h("e1")), (_id(2), _h("e2"))]) == 2
    assert _add(conn, did, "phone", [(_id(3), _h("p1"))]) == 1
    rows = _q(conn, "SELECT i.list_type, i.drop_id, i.hash, i.due_at - d.downloaded_at AS w, "
                    "i.removed_at FROM drop_identifiers i JOIN drop_downloads d "
                    "ON d.id = i.download_id WHERE d.id = :d ORDER BY i.drop_id", d=did).all()
    assert [(r.list_type, r.drop_id, r.hash) for r in rows] == [
        ("email", _id(1), _h("e1")), ("email", _id(2), _h("e2")), ("phone", _id(3), _h("p1"))]
    # The ledger stores exactly what the dated rule gives for this download's time.
    expected = _q(conn, "SELECT drop_due_at(downloaded_at) - downloaded_at FROM "
                        "drop_downloads WHERE id = :d", d=did).scalar()
    assert {r.w for r in rows} == {expected}
    assert all(r.removed_at is None for r in rows)
    counts = _q(conn, "SELECT list_counts FROM drop_downloads WHERE id = :d", d=did).scalar()
    assert counts == {"email": 2, "phone": 1}


@pytest.mark.parametrize(("downloaded_at", "days"), [
    ("2026-11-09 12:00:00-08", 45),
    ("2026-12-31 23:59:59-08", 45),   # the last Pacific moment of 2026
    ("2027-01-01 07:59:59+00", 45),   # the same moment, in UTC
    ("2027-01-01 00:00:00-08", 30),   # the first Pacific moment of 2027 (AB 883)
    ("2027-01-01 08:00:00+00", 30),
    ("2027-06-01 12:00:00-07", 30),
])
def test_the_report_window_is_45_days_before_2027_and_30_from_then(conn, downloaded_at,
                                                                     days):
    """COUNSEL TO CONFIRM (migration 139, drop_due_at): the date is California time."""
    did = str(_q(conn, "INSERT INTO drop_downloads (file_name, downloaded_at) "
                       "VALUES (:f, CAST(:t AS timestamptz)) RETURNING id",
                 f=_zip_name(), t=downloaded_at).scalar())
    assert _add(conn, did, "email", [(_id(1), _h("e1"))]) == 1
    window = _q(conn, "SELECT i.due_at - d.downloaded_at FROM drop_identifiers i "
                      "JOIN drop_downloads d ON d.id = i.download_id WHERE d.id = :d",
                d=did).scalar()
    assert window == timedelta(days=days)


def test_recording_the_same_download_again_changes_nothing(conn):
    name = _zip_name()
    did = _begin(conn, name)
    pairs = [(_id(1), _h("e1"))]
    assert _add(conn, did, "email", pairs) == 1
    assert _begin(conn, name) == did
    assert _add(conn, did, "email", pairs) == 0
    assert _q(conn, "SELECT count(*) FROM drop_identifiers WHERE download_id = :d",
              d=did).scalar() == 1


def test_a_pull_with_no_new_data_is_recorded_but_holds_no_identifiers(conn):
    first, second = _begin(conn, None), _begin(conn, None)
    assert first != second  # each pull is its own evidence of the cadence
    assert _q(conn, "SELECT drop_last_pull() >= max(downloaded_at) FROM drop_downloads "
                    "WHERE id = :d", d=first).scalar() is True
    assert _pgcode(conn, "SELECT drop_add_identifiers(CAST(:d AS uuid), 'email', "
                         "ARRAY['LedgerTst001'], ARRAY[:h])", d=first, h=_h("x")) == "BLD61"


def test_removed_marks_only_the_identifier_with_the_same_list_id_and_hash(conn):
    did = _begin(conn, _zip_name())
    _add(conn, did, "email", [(_id(1), _h("e1")), (_id(2), _h("e2"))])
    mark = ("SELECT drop_mark_removed(:t, CAST(:i AS text[]), CAST(:h AS text[]))")
    assert _q(conn, mark, t="email", i=[_id(1)], h=[_h("other")]).scalar() == 0
    assert _q(conn, mark, t="phone", i=[_id(1)], h=[_h("e1")]).scalar() == 0
    assert _q(conn, mark, t="email", i=[_id(1)], h=[_h("e1")]).scalar() == 1
    assert _q(conn, mark, t="email", i=[_id(1)], h=[_h("e1")]).scalar() == 0  # already
    pending = _q(conn, "SELECT drop_id FROM drop_pending_hashes(CAST(:d AS uuid), NULL, NULL, "
                       "100) p JOIN drop_identifiers i ON i.id = p.id", d=did).scalars().all()
    assert pending == [_id(2)]


@pytest.mark.parametrize(("list_type", "drop_id", "hash_", "code"), [
    ("email", "LedgerTst001", "not-a-hash", "23514"),
    ("email", "LedgerTst001", "A" * 43 + "=", None),            # valid shape: accepted
    ("email", "short", _h("x"), "23514"),
    ("dob", "LedgerTst001", _h("x"), "BLD61"),
])
def test_the_ledger_refuses_malformed_identifiers(conn, list_type, drop_id, hash_, code):
    did = _begin(conn, _zip_name())
    sql = ("SELECT drop_add_identifiers(CAST(:d AS uuid), :t, CAST(:i AS text[]), "
           "CAST(:h AS text[]))")
    params = {"d": did, "t": list_type, "i": [drop_id], "h": [hash_]}
    if code is None:
        assert conn.execute(text(sql), params).scalar() == 1
    else:
        assert _pgcode(conn, sql, **params) == code


def test_batches_are_bounded(conn):
    did = _begin(conn, _zip_name())
    ids = [f"Bound{n:07d}" for n in range(5001)]
    hashes = [_h(str(n)) for n in range(5001)]
    assert _pgcode(conn, "SELECT drop_add_identifiers(CAST(:d AS uuid), 'email', "
                         "CAST(:i AS text[]), CAST(:h AS text[]))",
                   d=did, i=ids, h=hashes) == "BLD61"
    assert _pgcode(conn, "SELECT drop_add_identifiers(CAST(:d AS uuid), 'email', "
                         "ARRAY['LedgerTst001'], ARRAY[]::text[])", d=did) == "BLD61"
    assert _pgcode(conn, "SELECT drop_begin_download('../x.zip', '{}', '{}', '{}')") == "BLD61"


def test_no_role_without_a_grant_reaches_any_of_it(conn):
    probe = f"r_probe_{uuid.uuid4().hex[:8]}"
    conn.execute(text(f"CREATE ROLE {probe} NOLOGIN"))
    for fn in _FUNCTIONS + ("drop_due_at(timestamptz)",):
        assert _q(conn, "SELECT has_function_privilege(:r, :f, 'EXECUTE')",
                  r=probe, f=f"public.{fn}").scalar() is False, fn
    for table in ("drop_downloads", "drop_identifiers"):
        for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert _q(conn, "SELECT has_table_privilege(:r, :t, :p)",
                      r=probe, t=f"public.{table}", p=priv).scalar() is False


def test_runtime_roles_hold_no_table_and_only_the_worker_executes(conn):
    roles = set(_q(conn, "SELECT rolname FROM pg_roles WHERE rolname IN ('bridgeleads_app', "
                         "'bridgeleads_system', 'anon', 'authenticated', 'service_role', "
                         "'bridgeleads_purge')").scalars())
    if not roles:
        pytest.skip("this cluster has none of the runtime roles")
    for role in roles:
        for table in ("drop_downloads", "drop_identifiers"):
            assert _q(conn, "SELECT has_table_privilege(:r, :t, 'SELECT')",
                      r=role, t=f"public.{table}").scalar() is False, (role, table)
        for fn in _FUNCTIONS:
            assert _q(conn, "SELECT has_function_privilege(:r, :f, 'EXECUTE')",
                      r=role, f=f"public.{fn}").scalar() is (role == "bridgeleads_system"), \
                (role, fn)
        assert _q(conn, "SELECT has_function_privilege(:r, 'public.drop_due_at(timestamptz)', "
                        "'EXECUTE')", r=role).scalar() is False, role


def test_rls_is_on_and_the_functions_are_definers_with_a_pinned_path(conn):
    assert set(_q(conn, "SELECT relname FROM pg_class WHERE relrowsecurity AND relname IN "
                        "('drop_downloads', 'drop_identifiers')").scalars()) == {
        "drop_downloads", "drop_identifiers"}
    for fn in _FUNCTIONS:
        row = _q(conn, "SELECT prosecdef, proconfig FROM pg_proc "
                       "WHERE oid = to_regprocedure(:f)", f=f"public.{fn}").one()
        assert row.prosecdef is True
        assert row.proconfig == ["search_path=pg_catalog, pg_temp"]


def test_the_grant_script_restores_exactly_these_grants() -> None:
    body = (Path(__file__).resolve().parent.parent
            / "scripts/provision_rls_roles.sql").read_text(encoding="utf-8")
    assert ("REVOKE ALL ON drop_downloads, drop_identifiers\n"
            "            FROM PUBLIC, bridgeleads_app, bridgeleads_system;") in body
    flat = " ".join(body.split())
    for fn in _FUNCTIONS:
        assert f"GRANT EXECUTE ON FUNCTION public.{fn} TO bridgeleads_system;" in flat, fn
    # After the blanket worker grant, or that grant would hand the tables back.
    assert body.index("ON ALL TABLES IN SCHEMA public TO bridgeleads_system") < body.index(
        "REVOKE ALL ON drop_downloads, drop_identifiers")
