"""Migration 130: results.privacy_checked_gen + its sweep index; King owner store tombstone.

Real database (schema from `alembic upgrade head`). Every test runs in one transaction
that is rolled back, so nothing persists; expected failures run in a SAVEPOINT.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.db.session import sync_engine

_PIN = "0123456789"


@pytest.fixture
def conn():
    with sync_engine.connect() as c:
        trans = c.begin()
        try:
            yield c
        finally:
            trans.rollback()


def _sqlstate(conn, sql: str, params: dict) -> str | None:
    sp = conn.begin_nested()
    try:
        conn.execute(text(sql), params)
    except DBAPIError as exc:
        sp.rollback()
        return getattr(exc.orig, "pgcode", None)
    sp.rollback()
    return None


def test_the_checked_generation_column_defaults_to_never_checked(conn):
    row = conn.execute(text(
        "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'results' "
        "AND column_name = 'privacy_checked_gen'")).one()
    assert (row.data_type, row.is_nullable) == ("bigint", "NO")
    assert row.column_default == "0"


def test_the_sweep_index_covers_unflagged_rows_by_generation_then_id(conn):
    row = conn.execute(text(
        "SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) AS def "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relname = 'ix_results_privacy_unchecked'")).one()
    valid, definition = row
    assert valid
    assert "(privacy_checked_gen, id)" in definition
    assert "WHERE (privacy_suppressed_at IS NULL)" in definition


_INSERT = ("INSERT INTO king_parcel_owners (parcel_id, outcome, owner_name, observed_at, source) "
           "VALUES (:p, :o, :n, now(), 'erealproperty')")


def test_a_privacy_tombstone_is_stored_without_a_name(conn):
    assert _sqlstate(conn, _INSERT, {"p": _PIN, "o": "privacy_withheld", "n": None}) is None


def test_a_privacy_tombstone_can_never_carry_a_name(conn):
    # 118's pairing CHECK: only 'found' may hold a name.
    assert _sqlstate(conn, _INSERT, {"p": _PIN, "o": "privacy_withheld",
                                     "n": "SMITH JOHN"}) == "23514"


def test_the_old_outcomes_still_work_and_an_unknown_one_is_refused(conn):
    assert _sqlstate(conn, _INSERT, {"p": _PIN, "o": "found", "n": "SMITH JOHN"}) is None
    assert _sqlstate(conn, _INSERT, {"p": _PIN, "o": "not_on_record", "n": None}) is None
    assert _sqlstate(conn, _INSERT, {"p": _PIN, "o": "withheld", "n": None}) == "23514"
