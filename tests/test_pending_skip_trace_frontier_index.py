"""Migration 103: the refill's keyset frontier index.

The index is trusted only by identity (the server's whole rendering plus
indisvalid), a wrong-shaped one of the same name on this table is rebuilt, and a
same-named index on another table is never touched. Schema comes from
`alembic upgrade head`.
"""
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from src.db.session import sync_engine


def _mig103():
    path = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
            / "103_pending_skip_trace_queued_frontier.py")
    spec = importlib.util.spec_from_file_location("_mig103", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    return mig


def _autocommit():
    return sync_engine.connect().execution_options(isolation_level="AUTOCOMMIT")


@pytest.fixture(autouse=True)
def _schema_as_103_left_it():
    """Two tests below replace or shadow the real index. `finally` restores it when
    a test fails, but not when the process dies mid-test, so every test here first
    puts the schema back exactly as 103 leaves it."""
    mig = _mig103()
    with _autocommit() as conn:
        stray = conn.execute(text(
            "SELECT t.relname FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
            "JOIN pg_class t ON t.oid = i.indrelid WHERE c.relname = :n"
        ), {"n": mig._INDEX}).scalar()
        if stray and stray != "pending_skip_trace_rows":
            conn.execute(text(f"DROP INDEX CONCURRENTLY public.{mig._INDEX}"))
        mig._build_frontier_index(conn)
    yield


def _indexdef(conn, name):
    return conn.execute(text(
        "SELECT pg_get_indexdef(i.indexrelid), i.indisvalid FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :n"
    ), {"n": name}).first()


def test_the_frontier_index_has_the_shape_the_refill_walks():
    mig = _mig103()
    with _autocommit() as conn:
        got = _indexdef(conn, mig._INDEX)
    assert got is not None
    indexdef, valid = got
    assert valid
    assert indexdef == mig._INDEX_DEF
    # trace_type leads: discovery for one type must not walk the other type's rows.
    assert "(trace_type, user_id, enqueued_at, id)" in indexdef
    assert "'queued'" in indexdef


def test_the_model_declares_what_the_migration_builds():
    from src.db.models import PendingSkipTraceRow

    idx = {i.name: i for i in PendingSkipTraceRow.__table__.indexes}
    ours = idx["ix_pending_skip_trace_queued_frontier"]
    assert [c.name for c in ours.columns] == ["trace_type", "user_id", "enqueued_at", "id"]
    assert "queued" in str(ours.dialect_options["postgresql"]["where"])


def test_autogenerate_never_proposes_a_blocking_build():
    import re

    env = (Path(__file__).resolve().parents[1] / "alembic" / "env.py").read_text()
    block = re.search(r"CONCURRENT_INDEXES = \{(.*?)\}", env, re.S).group(1)
    assert '"ix_pending_skip_trace_queued_frontier"' in block


def test_a_same_named_index_of_another_shape_is_rebuilt():
    # user_id leading would defeat per-type discovery; a name-only check would keep it.
    mig = _mig103()
    with _autocommit() as conn:
        try:
            conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS public.{mig._INDEX}"))
            conn.execute(text(
                f"CREATE INDEX CONCURRENTLY {mig._INDEX} ON public.pending_skip_trace_rows "
                f"(user_id, trace_type, enqueued_at, id) WHERE status = 'queued'"
            ))
            mig._build_frontier_index(conn)
            assert _indexdef(conn, mig._INDEX) == (mig._INDEX_DEF, True)
        finally:
            mig._build_frontier_index(conn)


def test_a_same_named_index_on_another_table_is_refused_not_dropped():
    mig = _mig103()
    with _autocommit() as conn:
        try:
            conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS public.{mig._INDEX}"))
            conn.execute(text(f"CREATE INDEX {mig._INDEX} ON public.results (id)"))
            with pytest.raises(RuntimeError, match="Migration 103 ABORTED"):
                mig._build_frontier_index(conn)
            owner = conn.execute(text(
                "SELECT t.relname FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
                "JOIN pg_class t ON t.oid = i.indrelid WHERE c.relname = :n"
            ), {"n": mig._INDEX}).scalar()
            assert owner == "results", "someone else's index must be left alone"
        finally:
            conn.execute(text(f"DROP INDEX IF EXISTS public.{mig._INDEX}"))
            mig._build_frontier_index(conn)
