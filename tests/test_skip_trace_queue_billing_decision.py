"""Migration 110: `skip_trace_queues.rows_sent` and `unmatched_billed` (Phase O-C).

Schema comes from `alembic upgrade head`. The down/up round trip runs inside ONE
transaction that is rolled back, so the shared test database is never left without
the columns (Postgres DDL is transactional). It re-runs `upgrade()` itself, so the
type, nullability AND absence of a default are checked on what the migration writes,
not only on the database it already built.
"""
import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from src.config import settings
from src.db.session import sync_engine, system_sync_session
from src.scrapers.enrichment import skip_trace
from src.workers import skip_trace_dispatcher as dispatcher
from tests.test_skip_trace_spend_ledger import _claim, _queue_id, _seed

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


# ── O-C-ii: the dispatcher records what each batch SENT ───────────────────────
# The REAL _persist_submission / _reconcile_stale_claims on real rows. Only the
# Tracerfy queue list (`fetch_queues`) is replaced: nothing leaves the process.


def _queue_cols(qid: int):
    with system_sync_session() as db:
        return tuple(db.execute(text(
            "SELECT rows_sent, rows_uploaded, unmatched_billed FROM skip_trace_queues "
            "WHERE tracerfy_queue_id = :q"), {"q": qid}).one())


def _stamped(qid: int) -> int:
    with system_sync_session() as db:
        return db.execute(text("SELECT count(*) FROM pending_skip_trace_rows "
                               "WHERE tracerfy_queue_id = :q"), {"q": qid}).scalar_one()


@pytest.fixture
def quiet_alerts(monkeypatch):
    monkeypatch.setattr(settings, "OPS_ALERT_EMAIL", "")


async def test_an_accepted_batch_records_what_it_sent(business_user):
    """Tracerfy de-duplicated one of three: the upload says 2, the batch sent 3."""
    claimed_at = datetime.now(UTC) - timedelta(minutes=1)
    rows = _seed(business_user.id, 3, status="submitting", submitted_at=claimed_at)
    qid = _queue_id()

    with system_sync_session() as db:
        dispatcher._persist_submission(
            db, qid, [_claim(r) for r in rows], "advanced",
            {"queue_id": qid, "rows_uploaded": 2}, claim_time=claimed_at,
        )

    assert _queue_cols(qid) == (3, 2, None)  # billing has not decided yet


async def test_partial_bookkeeping_still_records_the_full_batch(business_user, quiet_alerts):
    """THE W3 shape: one row was re-claimed since, so only 2 of the 3 sent are stamped
    with the queue. The stamped count is the number billing must NOT be judged by."""
    claimed_at = datetime.now(UTC) - timedelta(minutes=40)
    rows = _seed(business_user.id, 3, status="submitting", submitted_at=claimed_at)
    with system_sync_session() as db:
        db.execute(text("UPDATE pending_skip_trace_rows SET submitted_at = now() "
                        "WHERE id = :i"), {"i": rows[2]["pending"]})
        db.commit()
    qid = _queue_id()

    with system_sync_session() as db:
        dispatcher._persist_submission(
            db, qid, [_claim(r) for r in rows], "advanced",
            {"queue_id": qid, "rows_uploaded": 2}, claim_time=claimed_at,
        )

    assert _stamped(qid) == 2
    assert _queue_cols(qid)[0] == 3


async def test_a_second_bookkeeping_pass_keeps_the_first_count(business_user):
    """The fresh-session retry (or any re-run) finds the queue row already there: the
    ON CONFLICT keeps what the first write recorded."""
    claimed_at = datetime.now(UTC) - timedelta(minutes=2)
    rows = _seed(business_user.id, 2, status="submitting", submitted_at=claimed_at)
    qid = _queue_id()
    with system_sync_session() as db:
        dispatcher._persist_submission(
            db, qid, [_claim(r) for r in rows], "advanced",
            {"queue_id": qid, "rows_uploaded": 2}, claim_time=claimed_at,
        )

    assert dispatcher._persist_submission_retry(
        qid, [_claim(rows[0])], "advanced", {"queue_id": qid, "rows_uploaded": 1},
        claim_time=claimed_at,
    )

    assert _queue_cols(qid)[0] == 2


async def test_an_adopted_batch_records_the_stale_claims_size(
    business_user, quiet_alerts, monkeypatch,
):
    """Adoption rebuilds the batch from the rows still `submitting` at its claim time.
    Tracerfy reports 1 uploaded (it de-duplicated), the claim sent 2."""
    claimed_at = (datetime.now(UTC) - timedelta(days=1)).replace(microsecond=0)
    rows = _seed(business_user.id, 2, status="submitting", submitted_at=claimed_at)
    qid = _queue_id()
    monkeypatch.setattr(skip_trace, "fetch_queues", lambda *a, **k: [{
        "id": qid, "trace_type": "advanced", "queue_type": "api", "pending": False,
        "created_at": (claimed_at + timedelta(seconds=5)).isoformat(),
        "rows_uploaded": 1, "credits_deducted": 2, "download_url": None,
    }])

    with system_sync_session() as db:
        summary = dispatcher._reconcile_stale_claims(db)

    assert summary["adopted"] == 2
    assert _stamped(qid) == len(rows)
    assert _queue_cols(qid) == (2, 1, None)
