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


# The one foreign index these tests ever create, byte for byte. Anything else of
# the same name on another table is not ours and is never touched (Codex ii-c-1).
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
def _schema_as_103_left_it():
    """Two tests below replace or shadow the real index. `finally` restores it when
    a test fails, but not when the process dies mid-test, so every test here first
    puts the schema back exactly as 103 leaves it."""
    mig = _mig103()
    with _autocommit() as conn:
        # A constraint of exactly this name on this table only ever comes from the
        # exclusion test below, when the process died mid-test.
        conn.execute(text(
            f"ALTER TABLE public.pending_skip_trace_rows DROP CONSTRAINT IF EXISTS {mig._INDEX}"))
        for owner, indexdef in _same_named(conn, mig._INDEX):
            if owner == "public.pending_skip_trace_rows":
                continue
            if owner == "public.results" and indexdef == _test_artifact_def(mig._INDEX):
                # Left by the collision test when the process died mid-test.
                conn.execute(text(f"DROP INDEX public.{mig._INDEX}"))
            else:
                pytest.fail(f"{mig._INDEX} exists on {owner} and is not this suite's; "
                            f"refusing to touch it. Run against a disposable test DB.")
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


@pytest.mark.parametrize(("shape", "why"), [
    ("(user_id, trace_type, enqueued_at, id) WHERE status = 'queued'",
     "user_id leading defeats per-type discovery"),
    ("(trace_type, user_id, enqueued_at DESC, id) WHERE status = 'queued'",
     "a descending key walks the frontier backwards"),
    ("(trace_type, user_id, enqueued_at, id) INCLUDE (city) WHERE status = 'queued'",
     "an INCLUDE column is a different index"),
    ("(trace_type, user_id, enqueued_at, id) WHERE status = 'submitted'",
     "the wrong predicate covers the wrong rows"),
    ("(trace_type, user_id, enqueued_at, id)",
     "no predicate at all"),
])
def test_a_same_named_index_of_another_shape_is_rebuilt(shape, why):
    """Identity is structural (catalogs), not a rendered string, and a name-only
    check would keep every one of these."""
    mig = _mig103()
    with _autocommit() as conn:
        try:
            _drop_ours(conn, mig._INDEX)
            conn.execute(text(
                f"CREATE INDEX CONCURRENTLY {mig._INDEX} ON public.pending_skip_trace_rows "
                f"{shape}"
            ))
            wrong = conn.execute(text(mig._SHAPE_SQL), {"n": mig._INDEX}).first()
            assert not mig._is_right_shape(wrong), why
            mig._build_frontier_index(conn)
            assert mig._is_right_shape(
                conn.execute(text(mig._SHAPE_SQL), {"n": mig._INDEX}).first()), why
            assert _indexdef(conn, mig._INDEX) == (mig._INDEX_DEF, True)
        finally:
            mig._build_frontier_index(conn)


@pytest.mark.parametrize("rendered", [
    "((status)::text = 'queued'::text)",          # PostgreSQL 16 and 17
    "(status = 'queued'::text)",                  # a server that drops the column cast
    "((status)::character varying = 'queued')",   # or renders the other side's cast
])
def test_the_predicate_check_ignores_how_a_server_renders_casts(rendered):
    assert _mig103()._normalized_predicate(rendered) == "status='queued'"


@pytest.mark.parametrize("rendered", [
    "((status)::text = 'queued::text'::text)",    # a different literal
    "((status)::text = '(queued)'::text)",        # a different literal
    "((status)::text = 'queued'::text) OR true",  # a wider predicate
    "((status)::text <> 'queued'::text)",         # the opposite
])
def test_the_predicate_check_never_mistakes_a_different_predicate(rendered):
    # Casts and parentheses are stripped only OUTSIDE quoted literals.
    assert _mig103()._normalized_predicate(rendered) != "status='queued'"


def test_an_exclusion_constraint_under_our_name_aborts_and_is_left_alone():
    """An EXCLUDE constraint's index can match every column, order, opclass and the
    predicate; it is still not our index, and it cannot be dropped as an index."""
    mig = _mig103()
    with _autocommit() as conn:
        try:
            _drop_ours(conn, mig._INDEX)
            conn.execute(text(
                f"ALTER TABLE public.pending_skip_trace_rows ADD CONSTRAINT {mig._INDEX} "
                f"EXCLUDE USING btree (trace_type WITH =, user_id WITH =, enqueued_at WITH =, "
                f"id WITH =) WHERE (status = 'queued')"
            ))
            row = conn.execute(text(mig._SHAPE_SQL), {"n": mig._INDEX}).first()
            assert row.indisexclusion and row.backs_constraint
            assert not mig._is_right_shape(row)
            with pytest.raises(RuntimeError, match="backs a constraint"):
                mig._build_frontier_index(conn)
            assert conn.execute(text(
                "SELECT count(*) FROM pg_constraint WHERE conname = :n"
            ), {"n": mig._INDEX}).scalar() == 1, "the constraint must be left alone"
        finally:
            conn.execute(text(
                f"ALTER TABLE public.pending_skip_trace_rows DROP CONSTRAINT IF EXISTS {mig._INDEX}"))
            mig._build_frontier_index(conn)


def test_a_same_named_index_on_another_table_is_refused_not_dropped():
    mig = _mig103()
    with _autocommit() as conn:
        try:
            _drop_ours(conn, mig._INDEX)
            conn.execute(text(f"CREATE INDEX {mig._INDEX} ON public.results (id)"))
            assert _same_named(conn, mig._INDEX) == [
                ("public.results", _test_artifact_def(mig._INDEX))]
            with pytest.raises(RuntimeError, match="Migration 103 ABORTED"):
                mig._build_frontier_index(conn)
            owner = conn.execute(text(
                "SELECT t.relname FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
                "JOIN pg_class t ON t.oid = i.indrelid WHERE c.relname = :n"
            ), {"n": mig._INDEX}).scalar()
            assert owner == "results", "someone else's index must be left alone"
        finally:
            if (("public.results", _test_artifact_def(mig._INDEX))
                    in _same_named(conn, mig._INDEX)):
                conn.execute(text(f"DROP INDEX public.{mig._INDEX}"))
            mig._build_frontier_index(conn)


# ── Replay through the migration's own upgrade() / downgrade() ────────────────
#
# Driven through an Alembic MigrationContext bound to the suite's GUARDED test
# connection, never through alembic/env.py: env.py calls load_dotenv() and
# prefers DATABASE_URL_MIGRATE, which the test-DB guard does not pin, so a replay
# through it could reach whatever database a local .env names. The full revision
# chain is exercised by CI's `alembic upgrade head`.


def _run(step: str) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    mig = _mig103()
    with sync_engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        # As env.py runs it: inside Alembic's outer migration transaction, which
        # autocommit_block() must commit on entry (Codex ii-c-1 re-review).
        with Operations.context(ctx), ctx.begin_transaction():
            getattr(mig, step)()
        if conn.in_transaction():
            conn.commit()


def _state():
    mig = _mig103()
    with _autocommit() as conn:
        return _same_named(conn, mig._INDEX), _indexdef(conn, mig._INDEX)


def test_upgrade_downgrade_and_replay_converge_on_one_valid_index():
    mig = _mig103()
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


def test_an_invalid_index_left_by_a_failed_build_is_rebuilt():
    """A CONCURRENTLY build that fails leaves an INVALID index behind. Two rows of
    one trace_type make a UNIQUE build under our name fail genuinely; upgrade()
    must then drop the corpse and build the real index.

    Everything here is sync and committed: a concurrent build waits for every
    transaction holding a snapshot, so an idle-in-transaction async session (the
    `db` fixture after refresh()) would stall it into the 5 s lock_timeout. The
    account is created and deleted here instead of by that fixture."""
    import uuid

    from src.api.auth import hash_password
    from src.db.models import User
    from src.db.session import system_sync_session

    mig = _mig103()
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
            VALUES (:sc, :u, 'frontier', 'pierce', 'WA', 'probate', '[]'::json, '[]'::json,
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
                VALUES (:r, :j, :u, false, 'queued', :a, '{}'::json, now())
            """), {"r": rid, "j": job, "u": uid, "a": f"{n} FRONTIER ST"})
            db.execute(text("""
                INSERT INTO pending_skip_trace_rows (id, job_id, result_id, user_id,
                    property_address, city, state, trace_type, status, enqueued_at)
                VALUES (:p, :j, :r, :u, :a, 'TACOMA', 'WA', 'normal', 'queued', now())
            """), {"p": str(uuid.uuid4()), "j": job, "r": rid, "u": uid,
                   "a": f"{n} FRONTIER ST"})
        db.commit()
    try:
        with _autocommit() as conn:
            _drop_ours(conn, mig._INDEX)
            from sqlalchemy.exc import IntegrityError

            with pytest.raises(IntegrityError):  # two rows share trace_type 'normal'
                conn.execute(text(
                    f"CREATE UNIQUE INDEX CONCURRENTLY {mig._INDEX} "
                    f"ON public.pending_skip_trace_rows (trace_type)"))
            corpse = _indexdef(conn, mig._INDEX)
            assert corpse is not None and corpse[1] is False, "expected an INVALID corpse"
        _run("upgrade")
        assert _state()[1] == (mig._INDEX_DEF, True)
    finally:
        with system_sync_session() as db:
            db.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})  # rows cascade
            db.commit()
        _run("upgrade")
