"""Migration 105: the pause state's per-account spend-walk index.

Mirrors tests/test_pending_skip_trace_frontier_index.py (103): the index is trusted
only by structural identity plus indisvalid, a wrong-shaped one of the same name on
this table is rebuilt, and a same-named index on another table, or one backing a
constraint, is never touched. What is new is the INCLUDE column (Codex iii-c K1,
K7): the key list and the INCLUDE list are read apart, so every near miss between
them is caught. Schema comes from `alembic upgrade head`.
"""
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from src.db.session import sync_engine


def _mig105():
    path = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
            / "105_pending_skip_trace_account_spent.py")
    spec = importlib.util.spec_from_file_location("_mig105", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    return mig


def _autocommit():
    return sync_engine.connect().execution_options(isolation_level="AUTOCOMMIT")


# The one foreign index these tests ever create, byte for byte.
def _test_artifact_def(name: str) -> str:
    return f"CREATE INDEX {name} ON public.results USING btree (id)"


def _same_named(conn, name):
    """(schema.table, indexdef) of every index with this name, in any schema."""
    return [tuple(r) for r in conn.execute(text(
        "SELECT tn.nspname || '.' || t.relname, pg_get_indexdef(i.indexrelid) "
        "FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
        "JOIN pg_class t ON t.oid = i.indrelid JOIN pg_namespace tn ON tn.oid = t.relnamespace "
        "WHERE c.relname = :n"
    ), {"n": name}).all()]


def _drop_ours(conn, name):
    """Drop the index of this name ONLY where it sits on public.pending_skip_trace_rows."""
    if any(owner == "public.pending_skip_trace_rows" for owner, _ in _same_named(conn, name)):
        conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS public.{name}"))


@pytest.fixture(autouse=True)
def _schema_as_105_left_it():
    """Tests below replace or shadow the real index; `finally` restores it when a
    test fails, not when the process dies mid-test, so every test first puts the
    schema back exactly as 105 leaves it."""
    mig = _mig105()
    with _autocommit() as conn:
        conn.execute(text(
            f"ALTER TABLE public.pending_skip_trace_rows DROP CONSTRAINT IF EXISTS {mig._INDEX}"))
        for owner, indexdef in _same_named(conn, mig._INDEX):
            if owner == "public.pending_skip_trace_rows":
                continue
            if owner == "public.results" and indexdef == _test_artifact_def(mig._INDEX):
                conn.execute(text(f"DROP INDEX public.{mig._INDEX}"))
            else:
                pytest.fail(f"{mig._INDEX} exists on {owner} and is not this suite's; "
                            f"refusing to touch it. Run against a disposable test DB.")
        mig._build_account_spent_index(conn)
    yield


def _indexdef(conn, name):
    return conn.execute(text(
        "SELECT pg_get_indexdef(i.indexrelid), i.indisvalid FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :n"
    ), {"n": name}).first()


def _shape(conn, mig):
    return conn.execute(text(mig._SHAPE_SQL), {"n": mig._INDEX}).first()


def test_the_index_has_the_shape_the_pause_walk_needs():
    mig = _mig105()
    with _autocommit() as conn:
        got = _indexdef(conn, mig._INDEX)
        row = _shape(conn, mig)
    assert got == (mig._INDEX_DEF, True)
    assert mig._is_right_shape(row)
    # Keys and INCLUDE read apart (K1): 3 attributes, 2 of them keys.
    assert (row.indnatts, row.indnkeyatts) == (3, 2)
    assert list(row.key_cols) == ["user_id", "submitted_at"]
    assert list(row.include_cols) == ["trace_type"]


def test_the_model_declares_what_the_migration_builds():
    from src.db.models import PendingSkipTraceRow

    idx = {i.name: i for i in PendingSkipTraceRow.__table__.indexes}
    ours = idx["ix_pending_skip_trace_account_spent"]
    assert [c.name for c in ours.columns] == ["user_id", "submitted_at"]
    opts = ours.dialect_options["postgresql"]
    assert list(opts["include"]) == ["trace_type"]
    assert "submitted_at IS NOT NULL" in str(opts["where"])


def test_autogenerate_never_proposes_a_blocking_build():
    import re

    env = (Path(__file__).resolve().parents[1] / "alembic" / "env.py").read_text()
    block = re.search(r"CONCURRENT_INDEXES = \{(.*?)\}", env, re.S).group(1)
    assert '"ix_pending_skip_trace_account_spent"' in block


def test_a_replay_on_the_right_shape_does_not_rebuild():
    """Identity is structural, so the right index is kept, not dropped and rebuilt."""
    mig = _mig105()
    with _autocommit() as conn:
        before = conn.execute(text(
            "SELECT oid FROM pg_class WHERE relname = :n"), {"n": mig._INDEX}).scalar()
        mig._build_account_spent_index(conn)
        after = conn.execute(text(
            "SELECT oid FROM pg_class WHERE relname = :n"), {"n": mig._INDEX}).scalar()
    assert before == after


# The operator-class and collation checks cannot be exercised here: uuid and
# timestamptz have no non-default btree operator class in core PostgreSQL, and
# neither type is collatable (a COLLATE clause is an error). They stay as defence.
@pytest.mark.parametrize(("create", "why"), [
    ("CREATE INDEX CONCURRENTLY {n} ON public.pending_skip_trace_rows "
     "(user_id, submitted_at) WHERE submitted_at IS NOT NULL",
     "no INCLUDE: the weight is not index-only"),
    ("CREATE INDEX CONCURRENTLY {n} ON public.pending_skip_trace_rows "
     "(user_id, submitted_at) INCLUDE (city) WHERE submitted_at IS NOT NULL",
     "the wrong INCLUDE column"),
    ("CREATE INDEX CONCURRENTLY {n} ON public.pending_skip_trace_rows "
     "(user_id, submitted_at) INCLUDE (trace_type, city) WHERE submitted_at IS NOT NULL",
     "an extra INCLUDE column"),
    ("CREATE INDEX CONCURRENTLY {n} ON public.pending_skip_trace_rows "
     "(user_id, submitted_at, trace_type) WHERE submitted_at IS NOT NULL",
     "trace_type as a KEY, not INCLUDEd: same attributes, different index"),
    ("CREATE INDEX CONCURRENTLY {n} ON public.pending_skip_trace_rows "
     "(submitted_at, user_id) INCLUDE (trace_type) WHERE submitted_at IS NOT NULL",
     "keys reversed: cannot seek to an account"),
    ("CREATE INDEX CONCURRENTLY {n} ON public.pending_skip_trace_rows "
     "(user_id, submitted_at DESC) INCLUDE (trace_type) WHERE submitted_at IS NOT NULL",
     "a descending key walks the newest spend first"),
    ("CREATE INDEX CONCURRENTLY {n} ON public.pending_skip_trace_rows "
     "((user_id::text), submitted_at) INCLUDE (trace_type) WHERE submitted_at IS NOT NULL",
     "an expression key"),
    ("CREATE INDEX CONCURRENTLY {n} ON public.pending_skip_trace_rows "
     "(user_id, submitted_at) INCLUDE (trace_type) WHERE submitted_at IS NULL",
     "the opposite predicate covers the wrong rows"),
    ("CREATE INDEX CONCURRENTLY {n} ON public.pending_skip_trace_rows "
     "(user_id, submitted_at) INCLUDE (trace_type)",
     "no predicate at all"),
    ("CREATE UNIQUE INDEX CONCURRENTLY {n} ON public.pending_skip_trace_rows "
     "(user_id, submitted_at) INCLUDE (trace_type) WHERE submitted_at IS NOT NULL",
     "unique: a second claim in the same instant would fail to write"),
    ("CREATE INDEX CONCURRENTLY {n} ON public.pending_skip_trace_rows USING hash "
     "(user_id) WHERE submitted_at IS NOT NULL",
     "not a btree: no order to walk"),
])
def test_a_same_named_index_of_another_shape_is_rebuilt(create, why):
    mig = _mig105()
    with _autocommit() as conn:
        try:
            _drop_ours(conn, mig._INDEX)
            conn.execute(text(create.format(n=mig._INDEX)))
            assert not mig._is_right_shape(_shape(conn, mig)), why
            mig._build_account_spent_index(conn)
            assert mig._is_right_shape(_shape(conn, mig)), why
            assert _indexdef(conn, mig._INDEX) == (mig._INDEX_DEF, True)
        finally:
            mig._build_account_spent_index(conn)


@pytest.mark.parametrize("rendered", [
    "(submitted_at IS NOT NULL)",          # PostgreSQL 16 and 17
    "submitted_at IS NOT NULL",
    "((submitted_at) IS NOT NULL)",
])
def test_the_predicate_check_ignores_parentheses_and_spacing(rendered):
    assert _mig105()._normalized_predicate(rendered) == "submitted_atISNOTNULL"


@pytest.mark.parametrize("rendered", [
    "(submitted_at IS NULL)",
    "((submitted_at IS NOT NULL) OR true)",
    "(enqueued_at IS NOT NULL)",
    "(submitted_at IS NOT NULL AND status = 'queued'::text)",
])
def test_the_predicate_check_never_mistakes_a_different_predicate(rendered):
    assert _mig105()._normalized_predicate(rendered) != "submitted_atISNOTNULL"


def test_an_exclusion_constraint_under_our_name_aborts_and_is_left_alone():
    """An EXCLUDE constraint's index can match the keys, the INCLUDE list and the
    predicate; it is still not our index, and it cannot be dropped as an index."""
    mig = _mig105()
    with _autocommit() as conn:
        try:
            _drop_ours(conn, mig._INDEX)
            conn.execute(text(
                f"ALTER TABLE public.pending_skip_trace_rows ADD CONSTRAINT {mig._INDEX} "
                f"EXCLUDE USING btree (user_id WITH =, submitted_at WITH =) "
                f"INCLUDE (trace_type) WHERE (submitted_at IS NOT NULL)"
            ))
            row = _shape(conn, mig)
            assert row.indisexclusion and row.backs_constraint
            assert not mig._is_right_shape(row)
            with pytest.raises(RuntimeError, match="backs a constraint"):
                mig._build_account_spent_index(conn)
            assert conn.execute(text(
                "SELECT count(*) FROM pg_constraint WHERE conname = :n"
            ), {"n": mig._INDEX}).scalar() == 1, "the constraint must be left alone"
        finally:
            conn.execute(text(
                f"ALTER TABLE public.pending_skip_trace_rows DROP CONSTRAINT IF EXISTS {mig._INDEX}"))
            mig._build_account_spent_index(conn)


def test_a_same_named_index_on_another_table_is_refused_not_dropped():
    mig = _mig105()
    with _autocommit() as conn:
        try:
            _drop_ours(conn, mig._INDEX)
            conn.execute(text(f"CREATE INDEX {mig._INDEX} ON public.results (id)"))
            assert _same_named(conn, mig._INDEX) == [
                ("public.results", _test_artifact_def(mig._INDEX))]
            with pytest.raises(RuntimeError, match="Migration 105 ABORTED"):
                mig._build_account_spent_index(conn)
            owner = conn.execute(text(
                "SELECT t.relname FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
                "JOIN pg_class t ON t.oid = i.indrelid WHERE c.relname = :n"
            ), {"n": mig._INDEX}).scalar()
            assert owner == "results", "someone else's index must be left alone"
        finally:
            if (("public.results", _test_artifact_def(mig._INDEX))
                    in _same_named(conn, mig._INDEX)):
                conn.execute(text(f"DROP INDEX public.{mig._INDEX}"))
            mig._build_account_spent_index(conn)


# ── Replay through the migration's own upgrade() / downgrade() ────────────────
#
# Driven through an Alembic MigrationContext bound to the suite's GUARDED test
# connection, as 103's tests do; the full chain is exercised by CI's
# `alembic upgrade head`.


def _run(step: str, mig=None) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    mig = mig or _mig105()
    with sync_engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        # As env.py runs it: inside Alembic's outer migration transaction, which
        # autocommit_block() must commit on entry.
        with Operations.context(ctx), ctx.begin_transaction():
            getattr(mig, step)()
        if conn.in_transaction():
            conn.commit()


def _state():
    mig = _mig105()
    with _autocommit() as conn:
        return _same_named(conn, mig._INDEX), _indexdef(conn, mig._INDEX)


def test_upgrade_downgrade_and_replay_converge_on_one_valid_index():
    mig = _mig105()
    try:
        for step in ("downgrade", "upgrade", "downgrade", "upgrade", "upgrade"):
            _run(step)
            owners, got = _state()
            if step == "downgrade":
                assert owners == [], f"after {step}: {owners}"
            else:
                assert [o for o, _ in owners] == ["public.pending_skip_trace_rows"]
                assert got == (mig._INDEX_DEF, True), f"after {step}: {got}"
    finally:
        _run("upgrade")


def test_downgrade_leaves_a_same_named_index_on_another_table_alone():
    mig = _mig105()
    with _autocommit() as conn:
        _drop_ours(conn, mig._INDEX)
        conn.execute(text(f"CREATE INDEX {mig._INDEX} ON public.results (id)"))
    try:
        _run("downgrade")
        assert _state()[0] == [("public.results", _test_artifact_def(mig._INDEX))]
    finally:
        with _autocommit() as conn:
            if (("public.results", _test_artifact_def(mig._INDEX))
                    in _same_named(conn, mig._INDEX)):
                conn.execute(text(f"DROP INDEX public.{mig._INDEX}"))
        _run("upgrade")


_CREATE_OURS = (
    "CREATE INDEX CONCURRENTLY {n} ON public.pending_skip_trace_rows "
    "(user_id, submitted_at) INCLUDE (trace_type) WHERE submitted_at IS NOT NULL")


def _snapshot_holder():
    """Another session holding a REPEATABLE READ snapshot: it conflicts with none of
    a concurrent build's lock waits, but the build's last phase waits for it."""
    holder = sync_engine.connect().execution_options(isolation_level="REPEATABLE READ")
    holder.execute(text("SELECT 1"))
    return holder


def test_an_invalid_index_of_exactly_the_right_shape_is_rebuilt():
    """The corpse in the next test is also the wrong shape, so on its own it proves
    nothing about indisvalid. Here the build is cancelled in its LAST wait, for
    older snapshots, after it has marked the index ready, which leaves an index that
    is ready, of exactly our shape, and INVALID: only indisvalid can reject it.

    Deterministic (Codex 105 review): the build runs on its own connection in a
    thread; this test polls pg_stat_progress_create_index until that backend is
    'waiting for old snapshots' with the index ready and invalid, and only then
    cancels it. Every wait is bounded. (An idle READ COMMITTED reader keeps no
    snapshot between statements, so the build would not wait for it; a ROW
    EXCLUSIVE holder stops the build before the index is ready.)"""
    import threading
    import time

    mig = _mig105()
    with _autocommit() as conn:
        _drop_ours(conn, mig._INDEX)
    holder = _snapshot_holder()
    builder = sync_engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    pid = builder.execute(text("SELECT pg_backend_pid()")).scalar()
    outcome = {}

    def build():
        try:
            builder.execute(text(_CREATE_OURS.format(n=mig._INDEX)))
            outcome["result"] = "finished"
        except Exception as exc:  # noqa: BLE001 - the cancel is the expected outcome
            outcome["result"] = type(exc).__name__ + ": " + str(exc).splitlines()[0]

    worker = threading.Thread(target=build, daemon=True)
    try:
        worker.start()
        deadline = time.monotonic() + 20
        reached = False
        with _autocommit() as watch:
            while time.monotonic() < deadline and worker.is_alive():
                phase = watch.execute(text(
                    "SELECT phase FROM pg_stat_progress_create_index WHERE pid = :p"),
                    {"p": pid}).scalar()
                row = _shape(watch, mig)
                if (phase == "waiting for old snapshots" and row is not None
                        and row.indisready and not row.indisvalid):
                    reached = True
                    break
                time.sleep(0.05)
            assert reached, f"the build never reached its last wait: {outcome}"
            assert watch.execute(text("SELECT pg_cancel_backend(:p)"), {"p": pid}).scalar()
        worker.join(20)
        assert not worker.is_alive(), "the cancelled build did not return"
        assert "canceling statement" in outcome["result"], outcome
        with _autocommit() as conn:
            row = _shape(conn, mig)
            assert row is not None and not row.indisvalid, "expected an INVALID index"
            assert row.indisready and row.indislive, "cancelled in the last wait, not earlier"
            assert (list(row.key_cols), list(row.include_cols)) == (
                ["user_id", "submitted_at"], ["trace_type"]), "and exactly our shape"
            assert not mig._is_right_shape(row)
        holder.rollback()
        with _autocommit() as conn:
            mig._build_account_spent_index(conn)
            assert _indexdef(conn, mig._INDEX) == (mig._INDEX_DEF, True)
    finally:
        holder.rollback()
        holder.close()
        if worker.is_alive():
            with _autocommit() as conn:
                conn.execute(text("SELECT pg_cancel_backend(:p)"), {"p": pid})
            worker.join(20)
        builder.close()
        with _autocommit() as conn:
            mig._build_account_spent_index(conn)


def test_a_build_stalled_by_an_old_transaction_times_out_instead_of_hanging_the_boot():
    """lock_timeout does not end a CONCURRENTLY build's wait for older transactions;
    statement_timeout does (Codex 105 review). Shortened here so the test is quick;
    the upgrade must END (with a timeout), and the next run must converge."""
    import threading

    mig = _mig105()
    mig._BUILD_STATEMENT_TIMEOUT = "1s"
    with _autocommit() as conn:
        _drop_ours(conn, mig._INDEX)
    holder = _snapshot_holder()
    outcome = {}

    def upgrade():
        try:
            _run("upgrade", mig)
            outcome["result"] = "finished"
        except Exception as exc:  # noqa: BLE001 - the timeout is the expected outcome
            outcome["result"] = type(exc).__name__ + ": " + str(exc).splitlines()[0]

    worker = threading.Thread(target=upgrade, daemon=True)
    try:
        worker.start()
        worker.join(30)
        assert not worker.is_alive(), "the migration hung on an old transaction"
        assert "statement timeout" in outcome["result"], outcome
    finally:
        holder.rollback()
        holder.close()
        worker.join(30)
    _run("upgrade")
    assert _state()[1] == (mig._INDEX_DEF, True)


def test_an_invalid_index_left_by_a_failed_build_is_rebuilt():
    """A CONCURRENTLY build that fails leaves an INVALID index behind. Two spent rows
    of one account make a UNIQUE build on user_id under our name fail genuinely;
    upgrade() must then drop the corpse and build the real index. Everything is
    sync and committed (a concurrent build waits for every open snapshot)."""
    import uuid
    from datetime import UTC, datetime

    from src.api.auth import hash_password
    from src.db.models import User
    from src.db.session import system_sync_session

    mig = _mig105()
    uid = str(uuid.uuid4())
    with system_sync_session() as db:
        db.add(User(id=uid, email=f"test_{uid[:8]}@test.bridgeleads.io",
                    password_hash=hash_password("TestPass123!"), plan="business",
                    records_used=0, records_limit=5000))
        db.commit()
    with system_sync_session() as db:
        sc, job = str(uuid.uuid4()), str(uuid.uuid4())
        db.execute(text("""
            INSERT INTO scraper_configs (id, user_id, name, county, state, record_type, fields,
                enrichment, schedule, deliver, skip_trace_enabled, active)
            VALUES (:sc, :u, 'acct spent', 'pierce', 'WA', 'probate', '[]'::json, '[]'::json,
                    '{"frequency":"manual"}'::json, '{"format":"csv","emails":[]}'::json, true, true)
        """), {"sc": sc, "u": uid})
        db.execute(text("""
            INSERT INTO jobs (id, user_id, scraper_config_id, status, trigger, page_current,
                              page_total, record_count, retry_count)
            VALUES (:j, :u, :sc, 'done', 'manual', 0, 0, 0, 0)
        """), {"j": job, "u": uid, "sc": sc})
        for n in range(2):
            rid = str(uuid.uuid4())
            db.execute(text("""
                INSERT INTO results (id, job_id, user_id, is_duplicate, skip_trace_status,
                                     property_address, enrichment_data, created_at)
                VALUES (:r, :j, :u, false, 'submitted', :a, '{}'::json, now())
            """), {"r": rid, "j": job, "u": uid, "a": f"{n} ACCT SPENT ST"})
            db.execute(text("""
                INSERT INTO pending_skip_trace_rows (id, job_id, result_id, user_id,
                    property_address, city, state, trace_type, status, enqueued_at,
                    submitted_at, tracerfy_queue_id)
                VALUES (:p, :j, :r, :u, :a, 'TACOMA', 'WA', 'normal', 'submitted', now(),
                        :sub, 900001)
            """), {"p": str(uuid.uuid4()), "j": job, "r": rid, "u": uid,
                   "a": f"{n} ACCT SPENT ST", "sub": datetime.now(UTC)})
        db.commit()
    try:
        with _autocommit() as conn:
            _drop_ours(conn, mig._INDEX)
            from sqlalchemy.exc import IntegrityError

            with pytest.raises(IntegrityError):  # two spent rows share user_id
                conn.execute(text(
                    f"CREATE UNIQUE INDEX CONCURRENTLY {mig._INDEX} "
                    f"ON public.pending_skip_trace_rows (user_id) WHERE submitted_at IS NOT NULL"))
            corpse = _indexdef(conn, mig._INDEX)
            assert corpse is not None and corpse[1] is False, "expected an INVALID corpse"
        _run("upgrade")
        assert _state()[1] == (mig._INDEX_DEF, True)
    finally:
        with system_sync_session() as db:
            db.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})  # rows cascade
            db.commit()
        _run("upgrade")
