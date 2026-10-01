"""Migration 110: `skip_trace_queues.rows_sent` and `unmatched_billed` (Phase O-C).

Schema comes from `alembic upgrade head`. The down/up round trip runs inside ONE
transaction that is rolled back, so the shared test database is never left without
the columns (Postgres DDL is transactional). It re-runs `upgrade()` itself, so the
type, nullability AND absence of a default are checked on what the migration writes,
not only on the database it already built.
"""
import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from src.db.session import sync_engine

# (data_type, is_nullable, column_default). No default: NULL must keep meaning "not
# recorded" (legacy rows_sent) and "billing has not decided" (unmatched_billed).
EXPECTED = {
    "rows_sent": ("integer", "YES", None),
    "unmatched_billed": ("boolean", "YES", None),
}


def _mig110():
    path = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
            / "110_skip_trace_queue_billing_decision.py")
    spec = importlib.util.spec_from_file_location("_mig110", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    return mig


def _columns(conn) -> dict[str, tuple[str, str, str | None]]:
    rows = conn.execute(text(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'skip_trace_queues' "
        "AND column_name IN ('rows_sent', 'unmatched_billed')"
    )).all()
    return {r.column_name: (r.data_type, r.is_nullable, r.column_default) for r in rows}


def test_head_has_both_columns_nullable_typed_and_without_a_default():
    with sync_engine.connect() as conn:
        assert _columns(conn) == EXPECTED


def test_110_chains_on_109():
    mig = _mig110()
    assert (mig.revision, mig.down_revision) == ("110", "109")


def test_110_takes_a_lock_timeout_in_both_directions():
    """ADD/DROP COLUMN take ACCESS EXCLUSIVE on a table the dispatcher and ingest use:
    fail fast rather than queue them. Each direction is checked on its own (the setting
    is reset between them, since SET LOCAL lasts for the whole transaction)."""
    mig = _mig110()
    with sync_engine.connect() as conn:
        trans = conn.begin()
        try:
            with Operations.context(MigrationContext.configure(conn)):
                conn.execute(text("SET LOCAL lock_timeout = 0"))
                mig.downgrade()
                assert conn.execute(text("SHOW lock_timeout")).scalar() == "5s"
                conn.execute(text("SET LOCAL lock_timeout = 0"))
                mig.upgrade()
                assert conn.execute(text("SHOW lock_timeout")).scalar() == "5s"
        finally:
            trans.rollback()


@pytest.mark.asyncio
async def test_110_backfills_nothing(starter_user):
    """A queue recorded before 110 keeps NULL in both: its sent count is not
    recoverable and billing has not decided on it under O-C. A fabricated value would
    be billed against."""
    mig = _mig110()
    qid = int(uuid.uuid4().int % 10_000_000) + 910_000_000
    with sync_engine.connect() as conn:
        trans = conn.begin()
        try:
            with Operations.context(MigrationContext.configure(conn)):
                mig.downgrade()
                conn.execute(text(
                    "INSERT INTO skip_trace_queues (id, tracerfy_queue_id, user_id, "
                    "  trace_type, status, rows_uploaded, credits_deducted, submitted_at) "
                    "VALUES (CAST(:id AS uuid), :q, CAST(:u AS uuid), 'normal', "
                    "  'completed', 3, 3, now())"),
                    {"id": str(uuid.uuid4()), "q": qid, "u": starter_user.id})
                mig.upgrade()
            row = conn.execute(text(
                "SELECT rows_sent, unmatched_billed FROM skip_trace_queues "
                "WHERE tracerfy_queue_id = :q"), {"q": qid}).one()
            assert tuple(row) == (None, None)
        finally:
            trans.rollback()


def test_110_downgrade_drops_and_upgrade_restores_them():
    mig = _mig110()
    with sync_engine.connect() as conn:
        trans = conn.begin()
        try:
            with Operations.context(MigrationContext.configure(conn)):
                mig.downgrade()
                assert _columns(conn) == {}
                mig.upgrade()
            assert _columns(conn) == EXPECTED
        finally:
            trans.rollback()
