"""Migration 106: the done-time run-count breakdown columns on `jobs`.

Schema comes from `alembic upgrade head`. The down/up round trip runs inside ONE
transaction that is rolled back, so the shared test database is never left without
the columns (Postgres DDL is transactional).
"""
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from src.db.session import sync_engine

BREAKDOWN_COLUMNS = (
    "breakdown_dropped_before_save",
    "breakdown_no_address",
    "breakdown_same_run_merged",
    "breakdown_already_delivered",
    "breakdown_over_quota",
    "breakdown_new",
)


def _mig106():
    path = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
            / "106_jobs_run_breakdown.py")
    spec = importlib.util.spec_from_file_location("_mig106", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    return mig


def _columns(conn) -> dict[str, tuple[str, str]]:
    rows = conn.execute(text(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'jobs' "
        "AND column_name LIKE 'breakdown\\_%'"
    )).all()
    return {r.column_name: (r.data_type, r.is_nullable) for r in rows}


def test_head_has_six_nullable_integer_breakdown_columns():
    with sync_engine.connect() as conn:
        assert _columns(conn) == dict.fromkeys(BREAKDOWN_COLUMNS, ("integer", "YES"))


def test_106_chains_on_105():
    mig = _mig106()
    assert (mig.revision, mig.down_revision) == ("106", "105")


def test_106_downgrade_drops_and_upgrade_restores_them():
    mig = _mig106()
    with sync_engine.connect() as conn:
        trans = conn.begin()
        try:
            with Operations.context(MigrationContext.configure(conn)):
                mig.downgrade()
                assert _columns(conn) == {}
                mig.upgrade()
            assert _columns(conn) == dict.fromkeys(BREAKDOWN_COLUMNS, ("integer", "YES"))
        finally:
            trans.rollback()
