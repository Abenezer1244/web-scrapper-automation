"""Migration 102: a spent row's credit weight is a database fact.

The daily credit cap (1b-1b-ii) charges each claimed row by its trace_type
(normal = 1 Tracerfy credit, advanced = 2) over a rolling 24h window of
`submitted_at`. These tests try to do the two things that would let an account
spend credits the cap never saw, and require the DATABASE to refuse:

  * give a row a type the cap cannot weigh (CHECK);
  * re-type a row that has already been spent, which would rewrite what the cap
    charged it (trigger).

Schema comes from `alembic upgrade head` (not create_all). Each forbidden
statement gets its own test and the session is rolled back straight after the
failure, never reused (the 78-minute lesson in the 1b-1a handoff).
"""
import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from src.db.session import sync_engine, system_sync_session


def _mig102():
    path = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
            / "102_pending_skip_trace_spend_weight.py")
    spec = importlib.util.spec_from_file_location("_mig102", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    return mig


@pytest.fixture(autouse=True)
def _schema_as_102_left_it():
    """Three tests below replace real schema objects (the index, the CHECK) or add
    one (a test trigger). `finally` restores them when a test fails, but not when
    the process dies mid-test, and the database outlives the run. So every test
    here first puts the schema back exactly as 102 leaves it, and an interrupted
    run is healed by the next one instead of silently weakening every later run.
    """
    mig = _mig102()
    with sync_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("DROP TRIGGER IF EXISTS zz_test_retype ON pending_skip_trace_rows"))
        conn.execute(text("DROP FUNCTION IF EXISTS zz_test_retype_fn()"))
        condef = conn.execute(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :c "
            "AND conrelid = 'public.pending_skip_trace_rows'::regclass"
        ), {"c": mig._CHECK}).scalar()
        if condef is not None and condef.removesuffix(" NOT VALID") != mig._CHECK_DEF:
            conn.execute(text(
                f"ALTER TABLE public.pending_skip_trace_rows DROP CONSTRAINT {mig._CHECK}"
            ))
        mig._ensure_trace_type_check(conn)
        mig._build_spent_index(conn)
        conn.execute(text(mig._GUARD_FN))
        conn.execute(text(mig._TRIGGER_DDL))
    yield


def _pending(user_id: str, **row) -> str:
    """user -> job -> result -> one pending row, committed; returns the pending id."""
    sc_id, job_id, rid, pid = (str(uuid.uuid4()) for _ in range(4))
    cols = {"trace_type": "normal", "status": "queued", "submitted_at": None,
            "tracerfy_queue_id": None, **row}
    with system_sync_session() as db:
        db.execute(text("""
            INSERT INTO scraper_configs (id, user_id, name, county, state, record_type,
                fields, enrichment, schedule, deliver, skip_trace_enabled, active)
            VALUES (:sc, :u, 'weight test', 'pierce', 'WA', 'probate', '[]'::json,
                    '[]'::json, '{"frequency":"manual"}'::json,
                    '{"format":"csv","emails":[]}'::json, true, true)
        """), {"sc": sc_id, "u": user_id})
        db.execute(text("""
            INSERT INTO jobs (id, user_id, scraper_config_id, status, trigger, page_current,
                              page_total, record_count, retry_count)
            VALUES (:j, :u, :sc, 'done', 'manual', 0, 0, 0, 0)
        """), {"j": job_id, "u": user_id, "sc": sc_id})
        db.execute(text("""
            INSERT INTO results (id, job_id, user_id, is_duplicate, skip_trace_status,
                                 property_address, enrichment_data, created_at)
            VALUES (:r, :j, :u, false, 'queued', '1 WEIGHT ST', '{}'::json, now())
        """), {"r": rid, "j": job_id, "u": user_id})
        db.execute(text("""
            INSERT INTO pending_skip_trace_rows (id, job_id, result_id, user_id,
                property_address, city, state, trace_type, status, enqueued_at,
                submitted_at, tracerfy_queue_id)
            VALUES (:p, :j, :r, :u, '1 WEIGHT ST', 'TACOMA', 'WA', :tt, :st, now(), :sub, :q)
        """), {"p": pid, "j": job_id, "r": rid, "u": user_id, "tt": cols["trace_type"],
               "st": cols["status"], "sub": cols["submitted_at"], "q": cols["tracerfy_queue_id"]})
        db.commit()
    return pid


def _retype(pid: str, new_type: str, extra_set: str = "") -> None:
    with system_sync_session() as db:
        try:
            db.execute(text(
                f"UPDATE pending_skip_trace_rows SET trace_type = :t{extra_set} WHERE id = :p"
            ), {"t": new_type, "p": pid})
            db.commit()
        except Exception:
            db.rollback()
            raise


def _type_of(pid: str) -> str:
    with system_sync_session() as db:
        return db.execute(text("SELECT trace_type FROM pending_skip_trace_rows WHERE id = :p"),
                          {"p": pid}).scalar_one()


# ── Only weighable types exist ────────────────────────────────────────────────


async def test_a_row_the_cap_cannot_weigh_is_refused(starter_user):
    with pytest.raises(Exception) as exc:
        _pending(starter_user.id, trace_type="premium")
    assert "ck_pending_skip_trace_rows_trace_type" in str(exc.value)


# ── A spent row keeps its weight ─────────────────────────────────────────────


@pytest.mark.parametrize(("evidence", "why"), [
    ({"status": "submitting", "submitted_at": datetime.now(UTC)}, "claimed, outcome unknown"),
    ({"status": "submitted", "submitted_at": datetime.now(UTC), "tracerfy_queue_id": 424242},
     "accepted by Tracerfy"),
    ({"status": "errored", "tracerfy_queue_id": 434343},
     "legacy charged-but-unmatched: a queue id and no submit time"),
])
async def test_a_spent_row_cannot_be_retyped(starter_user, evidence, why):
    pid = _pending(starter_user.id, trace_type="advanced", **evidence)
    with pytest.raises(Exception) as exc:
        _retype(pid, "normal")
    assert "trace_type of a spent row cannot change" in str(exc.value), why
    assert _type_of(pid) == "advanced"


async def test_retyping_while_stamping_the_spend_in_one_update_is_refused(starter_user):
    # The NEW row carries the evidence even though the OLD one did not.
    pid = _pending(starter_user.id, trace_type="advanced")
    with pytest.raises(Exception) as exc:
        _retype(pid, "normal", ", status = 'submitting', submitted_at = now()")
    assert "trace_type of a spent row cannot change" in str(exc.value)
    assert _type_of(pid) == "advanced"


async def test_an_unsent_row_may_still_change_type(starter_user):
    # The probate repair's name refresh does exactly this, on queued rows with no
    # submission evidence; it must keep working.
    pid = _pending(starter_user.id, trace_type="advanced")
    _retype(pid, "normal")
    assert _type_of(pid) == "normal"


async def test_the_spend_itself_is_not_blocked(starter_user):
    # The dispatcher's claim and bookkeeping never touch trace_type, so the
    # trigger's WHEN (trace_type changed) is false and it never runs for them.
    pid = _pending(starter_user.id, trace_type="advanced")
    with system_sync_session() as db:
        db.execute(text("UPDATE pending_skip_trace_rows SET status = 'submitting', "
                        "submitted_at = now() WHERE id = :p"), {"p": pid})
        db.execute(text("UPDATE pending_skip_trace_rows SET status = 'submitted', "
                        "tracerfy_queue_id = 454545 WHERE id = :p"), {"p": pid})
        db.commit()
    assert _type_of(pid) == "advanced"


async def test_an_upsert_cannot_retype_a_spent_row(starter_user):
    # ON CONFLICT DO UPDATE takes the UPDATE path, so it must meet the same guard.
    pid = _pending(starter_user.id, trace_type="advanced", status="submitting",
                   submitted_at=datetime.now(UTC))
    with system_sync_session() as db:
        with pytest.raises(Exception) as exc:
            db.execute(text(
                "INSERT INTO pending_skip_trace_rows SELECT * FROM pending_skip_trace_rows "
                "WHERE id = :p ON CONFLICT (id) DO UPDATE SET trace_type = 'normal'"
            ), {"p": pid})
        db.rollback()
    assert "trace_type of a spent row cannot change" in str(exc.value)
    assert _type_of(pid) == "advanced"


async def test_writing_the_same_type_to_a_spent_row_is_not_a_change(starter_user):
    pid = _pending(starter_user.id, trace_type="advanced", status="submitting",
                   submitted_at=datetime.now(UTC))
    _retype(pid, "advanced")
    assert _type_of(pid) == "advanced"


async def test_a_retype_made_by_another_before_trigger_is_still_refused(starter_user):
    # A BEFORE trigger sees NEW only as far as the triggers ahead of it have
    # built it, and a statement that never names trace_type skips a column
    # trigger entirely. The guard is AFTER, on the row as finally written, so a
    # later trigger (here one that rewrites the type on a city update) cannot
    # smuggle a retype past it. This one exists only for the test's duration.
    pid = _pending(starter_user.id, trace_type="advanced", status="submitting",
                   submitted_at=datetime.now(UTC))
    with system_sync_session() as db:
        db.execute(text(
            "CREATE FUNCTION zz_test_retype_fn() RETURNS trigger AS $f$ BEGIN "
            "NEW.trace_type := 'normal'; RETURN NEW; END; $f$ LANGUAGE plpgsql"
        ))
        db.execute(text(
            "CREATE TRIGGER zz_test_retype BEFORE UPDATE OF city ON pending_skip_trace_rows "
            "FOR EACH ROW EXECUTE FUNCTION zz_test_retype_fn()"
        ))
        db.commit()
    try:
        with system_sync_session() as db:
            with pytest.raises(Exception) as exc:
                db.execute(text("UPDATE pending_skip_trace_rows SET city = 'SEATTLE' "
                                "WHERE id = :p"), {"p": pid})
            db.rollback()
        assert "trace_type of a spent row cannot change" in str(exc.value)
        assert _type_of(pid) == "advanced"
    finally:
        with system_sync_session() as db:
            db.execute(text("DROP TRIGGER IF EXISTS zz_test_retype ON pending_skip_trace_rows"))
            db.execute(text("DROP FUNCTION IF EXISTS zz_test_retype_fn()"))
            db.commit()


# ── The migration trusts objects by identity, not by name ────────────────────


def test_a_same_named_index_of_another_shape_is_rebuilt():
    # Descending order is invisible to a column-list check but not to the
    # server's own rendering of the index.
    mig = _mig102()
    with sync_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        try:
            conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS public.{mig._INDEX}"))
            conn.execute(text(
                f"CREATE INDEX CONCURRENTLY {mig._INDEX} ON public.pending_skip_trace_rows "
                f"(submitted_at DESC) INCLUDE (user_id, trace_type) WHERE submitted_at IS NOT NULL"
            ))
            mig._build_spent_index(conn)
            got = conn.execute(text(
                f"SELECT pg_get_indexdef('public.{mig._INDEX}'::regclass)"
            )).scalar_one()
            assert got == mig._INDEX_DEF
        finally:
            mig._build_spent_index(conn)


def test_a_same_named_check_that_says_something_else_aborts():
    mig = _mig102()
    with sync_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        try:
            conn.execute(text(
                f"ALTER TABLE public.pending_skip_trace_rows DROP CONSTRAINT {mig._CHECK}"
            ))
            conn.execute(text(
                f"ALTER TABLE public.pending_skip_trace_rows ADD CONSTRAINT {mig._CHECK} "
                f"CHECK (trace_type IN ('normal', 'advanced', 'premium')) NOT VALID"
            ))
            with pytest.raises(RuntimeError, match="Migration 102 ABORTED"):
                mig._ensure_trace_type_check(conn)
        finally:
            conn.execute(text(
                f"ALTER TABLE public.pending_skip_trace_rows DROP CONSTRAINT IF EXISTS {mig._CHECK}"
            ))
            mig._ensure_trace_type_check(conn)
        assert conn.execute(text(
            "SELECT convalidated FROM pg_constraint WHERE conname = :c"
        ), {"c": mig._CHECK}).scalar_one()


# ── The cap's spent query has its index ──────────────────────────────────────


async def test_the_spent_index_has_the_shape_the_cap_reads(starter_user):
    with system_sync_session() as db:
        row = db.execute(text(
            "SELECT i.indisvalid, pg_get_expr(i.indpred, i.indrelid) AS predicate, "
            "       ARRAY(SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY k(n, o) "
            "             JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.n "
            "             ORDER BY k.o) AS cols, i.indnkeyatts "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = 'ix_pending_skip_trace_spent'"
        )).one()
    assert row.indisvalid
    assert row.predicate == "(submitted_at IS NOT NULL)"
    assert list(row.cols) == ["submitted_at", "user_id", "trace_type"]
    assert row.indnkeyatts == 1  # user_id and trace_type are INCLUDE payload, not keys
