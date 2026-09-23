"""Migration 101: the contact-lookup ledger's schema, and the two guarantees it exists for.

Phase 1b-1a is schema only — no endpoint, no writer. So these tests assert the
DATABASE's own behavior rather than any application code, because right now the
database is the only thing enforcing anything:

  1. A child row can never point at another account's action or lead. That is the
     tenant-carrying COMPOSITE foreign key, and it is the reason `jobs`, `results`
     and `contact_lookup_actions` each grow a UNIQUE (id, user_id) (finding 16-1;
     15-9 named `results` alone, so the FK it specified could not have been
     created at all).
  2. A user-scoped session may CREATE an initial verdict and may never transition
     one. Grants bound which table a role may touch and RLS bounds which rows;
     neither can express that rule, so a trigger does (finding 16-6).

Both are tested by trying to do the forbidden thing and requiring the database to
refuse it. A test that only asserted the objects EXIST would pass against a
constraint that enforces nothing.

Schema comes from `alembic upgrade head`, which is how both the local rig and CI
build the test database — NOT from `create_all`, whatever the comments in
models.py, alembic/env.py and migrations 049/089 claim. Nothing in this
repository calls `create_all`.
"""
import re
import uuid

import pytest
from sqlalchemy import text

from src.db.models import (
    CONTACT_LOOKUP_ACTION_STATUSES,
    CONTACT_LOOKUP_API_INITIAL_DISPOSITIONS,
    CONTACT_LOOKUP_DISPOSITIONS,
    RESULT_TRACE_OUTCOMES,
    Job,
    Result,
    ScraperConfig,
    User,
)

_SITUS = {"property_city": "VANCOUVER", "property_state": "WA", "property_zip": "98661"}


async def _job_and_result(db, user: User) -> tuple[str, str]:
    cfg = ScraperConfig(
        id=str(uuid.uuid4()), user_id=user.id, name="cl-schema",
        county="clark", state="WA", record_type="probate",
        fields=[], enrichment=[], schedule={"frequency": "manual"},
        deliver={"formats": ["csv"], "emails": []},
    )
    db.add(cfg)
    job_id = str(uuid.uuid4())
    db.add(Job(id=job_id, user_id=user.id, scraper_config_id=cfg.id,
               status="done", trigger="manual"))
    rid = str(uuid.uuid4())
    db.add(Result(id=rid, job_id=job_id, user_id=user.id,
                  property_address="1400 MAIN ST", is_duplicate=False, **_SITUS))
    await db.commit()
    return job_id, rid


async def _action(db, user: User, job_id: str) -> str:
    aid = str(uuid.uuid4())
    await db.execute(text(
        "INSERT INTO contact_lookup_actions "
        "(id, user_id, job_id, category, quote_id, status, unit_price_cents, "
        " currency, pricing_version) "
        "VALUES (:id, :uid, :job, 'tab', :q, 'dispatching', 8, 'usd', 'v1')"
    ), {"id": aid, "uid": user.id, "job": job_id, "q": f"q-{aid}"})
    await db.commit()
    return aid


async def test_the_parent_uniques_are_constraints_not_merely_indexes(db):
    """A foreign key cannot target a bare unique index, only a constraint.

    Migration 101 builds each one CONCURRENTLY and then promotes it with
    ADD CONSTRAINT ... USING INDEX precisely so the FK has something to point at
    without a full-table rescan. If a future change ever "simplifies" these back
    to plain indexes, the composite FKs below stop being creatable — so this
    asserts contype='u', not merely that the name exists.
    """
    rows = (await db.execute(text(
        "SELECT conname, contype FROM pg_constraint "
        "WHERE conname IN ('uq_jobs_id_user', 'uq_results_id_user', "
        "                  'uq_contact_lookup_actions_id_user')"
    ))).all()
    # asyncpg hands back Postgres "char" as bytes, psycopg2 as str; normalise
    # rather than assert on the driver's representation.
    found = {
        r.conname: (r.contype.decode() if isinstance(r.contype, bytes) else r.contype)
        for r in rows
    }
    for name in ("uq_jobs_id_user", "uq_results_id_user",
                 "uq_contact_lookup_actions_id_user"):
        assert name in found, f"{name} is missing: no composite FK can target it"
        assert found[name] == "u", f"{name} is not a UNIQUE CONSTRAINT (contype={found[name]!r})"


async def test_a_verdict_cannot_point_at_another_accounts_lead(db, business_user, starter_user):
    """THE tenant guarantee. Not a style preference — the FK is the only thing
    that makes cross-tenant attachment impossible at the database, below RLS and
    below every application-layer filter."""
    job_id, _mine = await _job_and_result(db, business_user)
    _other_job, theirs = await _job_and_result(db, starter_user)
    action = await _action(db, business_user, job_id)

    with pytest.raises(Exception) as exc:
        await db.execute(text(
            "INSERT INTO contact_lookup_action_results "
            "(id, action_id, user_id, result_id, disposition) "
            "VALUES (:id, :a, :uid, :r, 'quoted')"
        ), {"id": str(uuid.uuid4()), "a": action,
            "uid": business_user.id, "r": theirs})
        await db.commit()
    # The composite FK is (result_id, user_id) -> results(id, user_id): the lead
    # exists, but not under THIS tenant, so the pair matches nothing.
    assert "fk_contact_lookup_action_results_result_tenant" in str(exc.value).lower() \
        or "foreign key" in str(exc.value).lower()
    await db.rollback()


async def test_an_action_cannot_point_at_another_accounts_job(db, business_user, starter_user):
    _mine, _r = await _job_and_result(db, business_user)
    their_job, _tr = await _job_and_result(db, starter_user)

    with pytest.raises(Exception) as exc:
        await db.execute(text(
            "INSERT INTO contact_lookup_actions "
            "(id, user_id, job_id, category, quote_id, status, unit_price_cents, "
            " currency, pricing_version) "
            "VALUES (:id, :uid, :job, 'tab', :q, 'dispatching', 8, 'usd', 'v1')"
        ), {"id": str(uuid.uuid4()), "uid": business_user.id,
            "job": their_job, "q": f"q-{uuid.uuid4()}"})
        await db.commit()
    assert "foreign key" in str(exc.value).lower() or "fk_contact_lookup_actions_job_tenant" in str(exc.value)
    await db.rollback()


async def test_a_user_scoped_session_may_create_an_initial_verdict(db, business_user):
    """The permitted half. Without this the next test could pass because the
    trigger refuses EVERYTHING, which would be a different bug."""
    job_id, rid = await _job_and_result(db, business_user)
    action = await _action(db, business_user, job_id)

    await db.execute(text("SELECT set_config('app.current_user_id', :uid, true)"),
                     {"uid": str(business_user.id)})
    await db.execute(text(
        "INSERT INTO contact_lookup_action_results "
        "(id, action_id, user_id, result_id, disposition) "
        "VALUES (:id, :a, :uid, :r, 'quoted')"
    ), {"id": str(uuid.uuid4()), "a": action, "uid": business_user.id, "r": rid})
    n = (await db.execute(text(
        "SELECT count(*) FROM contact_lookup_action_results WHERE action_id = :a"
    ), {"a": action})).scalar_one()
    assert n == 1
    await db.rollback()


@pytest.mark.parametrize("terminal", ["answered_hit", "unmatched_billable", "newly_queued"])
async def test_a_user_scoped_session_cannot_write_a_worker_verdict(db, business_user, terminal):
    """The API must not be able to declare a lead answered, billable or queued.

    Every one of these is a verdict only the worker may reach, and two of them
    are BILLABLE — an API that could write them could fabricate a charge, or
    hide one.
    """
    job_id, rid = await _job_and_result(db, business_user)
    action = await _action(db, business_user, job_id)

    await db.execute(text("SELECT set_config('app.current_user_id', :uid, true)"),
                     {"uid": str(business_user.id)})
    with pytest.raises(Exception) as exc:
        await db.execute(text(
            "INSERT INTO contact_lookup_action_results "
            "(id, action_id, user_id, result_id, disposition) "
            "VALUES (:id, :a, :uid, :r, :d)"
        ), {"id": str(uuid.uuid4()), "a": action, "uid": business_user.id,
            "r": rid, "d": terminal})
    assert "initial disposition" in str(exc.value)
    await db.rollback()


async def test_a_user_scoped_session_cannot_transition_a_verdict(db, business_user):
    """Creating is allowed, moving is not. This is the half that grants and RLS
    genuinely cannot express: both would happily permit an UPDATE of the tenant's
    own row."""
    job_id, rid = await _job_and_result(db, business_user)
    action = await _action(db, business_user, job_id)
    # Written WITHOUT the GUC, i.e. as the worker does.
    await db.execute(text(
        "INSERT INTO contact_lookup_action_results "
        "(id, action_id, user_id, result_id, disposition) "
        "VALUES (:id, :a, :uid, :r, 'quoted')"
    ), {"id": str(uuid.uuid4()), "a": action, "uid": business_user.id, "r": rid})
    await db.commit()

    await db.execute(text("SELECT set_config('app.current_user_id', :uid, true)"),
                     {"uid": str(business_user.id)})
    with pytest.raises(Exception) as exc:
        await db.execute(text(
            "UPDATE contact_lookup_action_results SET disposition = 'answered_hit' "
            "WHERE action_id = :a"
        ), {"a": action})
    assert "may not transition" in str(exc.value)
    await db.rollback()


async def test_the_worker_may_write_any_verdict(db, business_user):
    """A system session sets no tenant GUC at all, which is how the trigger tells
    the two apart. If this ever fails, the action worker is opening its session
    with rls_sync_session() instead of system_sync_session() — it fails CLOSED,
    which is the right direction but looks like a baffling permission error."""
    job_id, rid = await _job_and_result(db, business_user)
    action = await _action(db, business_user, job_id)
    row = str(uuid.uuid4())
    await db.execute(text(
        "INSERT INTO contact_lookup_action_results "
        "(id, action_id, user_id, result_id, disposition) "
        "VALUES (:id, :a, :uid, :r, 'quoted')"
    ), {"id": row, "a": action, "uid": business_user.id, "r": rid})
    await db.execute(text(
        "UPDATE contact_lookup_action_results SET disposition = 'answered_hit' "
        "WHERE id = :id"
    ), {"id": row})
    got = (await db.execute(text(
        "SELECT disposition FROM contact_lookup_action_results WHERE id = :id"
    ), {"id": row})).scalar_one()
    assert got == "answered_hit"
    await db.rollback()


async def test_the_check_constraints_match_the_python_vocabularies(db):
    """The CHECK is the enforcement; the Python tuple is what application code
    reads. If they drift, code can build a value the database refuses at write
    time — in the worker, mid-claim, after money has been spent."""
    for const, values in (
        ("ck_contact_lookup_action_results_disposition", CONTACT_LOOKUP_DISPOSITIONS),
        ("ck_contact_lookup_actions_status", CONTACT_LOOKUP_ACTION_STATUSES),
        ("ck_results_last_trace_outcome", RESULT_TRACE_OUTCOMES),
    ):
        src = (await db.execute(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :c"
        ), {"c": const})).scalar_one()
        # Exact set equality, both directions. A value in Python but not in the
        # CHECK fails at write time, in the worker, after money has been spent; a
        # value in the CHECK but not in Python is a state the database will
        # accept and no code knows how to handle.
        in_constraint = {
            m for m in re.findall(r"'([^']+)'", src)
            if not m.startswith("character")
        }
        assert in_constraint == set(values), (
            f"{const} and its Python tuple have drifted: "
            f"only in DB={sorted(in_constraint - set(values))}, "
            f"only in Python={sorted(set(values) - in_constraint)}"
        )


async def test_the_api_initial_set_is_a_strict_subset_of_the_vocabulary(db):
    """A typo here would either let the API write a worker verdict or refuse a
    legitimate exclusion, and the trigger renders this tuple straight into SQL."""
    assert set(CONTACT_LOOKUP_API_INITIAL_DISPOSITIONS) < set(CONTACT_LOOKUP_DISPOSITIONS)
    for terminal in ("answered_hit", "answered_miss", "unmatched_billable",
                     "newly_queued", "reused", "released", "abandoned"):
        assert terminal not in CONTACT_LOOKUP_API_INITIAL_DISPOSITIONS


async def test_the_migration_and_the_models_have_not_drifted(db):
    """Migration 101 keeps its OWN copy of every vocabulary, deliberately: a
    migration must keep working when application code moves on (the convention
    migration 100 set). Deliberate duplication still needs an assertion, or it is
    just duplication."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / \
        "101_contact_lookup_action_schema.py"
    spec = importlib.util.spec_from_file_location("_mig101", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    assert tuple(mig._DISPOSITIONS) == tuple(CONTACT_LOOKUP_DISPOSITIONS)
    assert tuple(mig._API_INITIAL_DISPOSITIONS) == tuple(CONTACT_LOOKUP_API_INITIAL_DISPOSITIONS)
    assert tuple(mig._ACTION_STATUSES) == tuple(CONTACT_LOOKUP_ACTION_STATUSES)
    assert tuple(mig._TRACE_OUTCOMES) == tuple(RESULT_TRACE_OUTCOMES)


async def test_rls_is_enabled_on_all_three_tables(db):
    rows = (await db.execute(text(
        "SELECT tablename, rowsecurity FROM pg_tables "
        "WHERE tablename LIKE 'contact_lookup%'"
    ))).all()
    assert len(rows) == 3, f"expected 3 ledger tables, found {[r.tablename for r in rows]}"
    for r in rows:
        assert r.rowsecurity, f"{r.tablename} does not have RLS enabled"


async def test_last_trace_outcome_ships_unwritten_and_nullable(db):
    """Finding 16-3: the column lands ahead of its writers on purpose, and NULL
    means UNKNOWN. A NOT NULL or a default here would be a claim about eight live
    paid-state transitions that do not write it yet."""
    col = (await db.execute(text(
        "SELECT is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'results' AND column_name = 'last_trace_outcome'"
    ))).one()
    assert col.is_nullable == "YES"
    assert col.column_default is None, "a default would fabricate an outcome nobody measured"


async def test_a_user_scoped_session_may_append_only_the_dispatching_event(db, business_user):
    """The event log is what a disputed charge is argued from, so append-only has
    to mean the API cannot append a LIE either. A grant stops it EDITING history
    and cannot stop it WRITING a fictional entry."""
    job_id, _rid = await _job_and_result(db, business_user)
    action = await _action(db, business_user, job_id)
    await db.execute(text("SELECT set_config('app.current_user_id', :uid, true)"),
                     {"uid": str(business_user.id)})
    # The one hop the API owns.
    await db.execute(text(
        "INSERT INTO contact_lookup_action_events (id, action_id, user_id, to_status) "
        "VALUES (:id, :a, :uid, 'dispatching')"
    ), {"id": str(uuid.uuid4()), "a": action, "uid": business_user.id})
    n = (await db.execute(text(
        "SELECT count(*) FROM contact_lookup_action_events WHERE action_id = :a"
    ), {"a": action})).scalar_one()
    assert n == 1
    await db.rollback()


@pytest.mark.parametrize("cols,vals,why", [
    ("to_status", "'settled'", "a terminal hop the worker owns"),
    ("to_status, from_status", "'running', 'dispatching'", "a worker transition"),
])
async def test_a_user_scoped_session_cannot_forge_history(db, business_user, cols, vals, why):
    job_id, _rid = await _job_and_result(db, business_user)
    action = await _action(db, business_user, job_id)
    await db.execute(text("SELECT set_config('app.current_user_id', :uid, true)"),
                     {"uid": str(business_user.id)})
    with pytest.raises(Exception) as exc:
        await db.execute(text(
            f"INSERT INTO contact_lookup_action_events (id, action_id, user_id, {cols}) "
            f"VALUES (:id, :a, :uid, {vals})"
        ), {"id": str(uuid.uuid4()), "a": action, "uid": business_user.id})
    assert "initial dispatching event" in str(exc.value), why
    await db.rollback()


async def test_a_user_scoped_session_cannot_claim_a_per_lead_hop(db, business_user):
    """A per-lead event (a release, an abandonment) carries a result_id the API
    has no business asserting: it is the worker saying what happened to one
    lead's money."""
    job_id, rid = await _job_and_result(db, business_user)
    action = await _action(db, business_user, job_id)
    await db.execute(text("SELECT set_config('app.current_user_id', :uid, true)"),
                     {"uid": str(business_user.id)})
    with pytest.raises(Exception) as exc:
        await db.execute(text(
            "INSERT INTO contact_lookup_action_events "
            "(id, action_id, user_id, result_id, to_status) "
            "VALUES (:id, :a, :uid, :r, 'dispatching')"
        ), {"id": str(uuid.uuid4()), "a": action, "uid": business_user.id, "r": rid})
    assert "initial dispatching event" in str(exc.value)
    await db.rollback()


async def test_a_user_scoped_session_cannot_rewrite_an_existing_event(db, business_user):
    """Append-only has to hold against EDITING, not only against forging.

    Only UPDATE is exercised. DELETE is refused by the same trigger branch, but
    asserting it here once cost a 78-minute run: the statement blocked on a row
    lock, and continuing to use the session after a failed statement then raised
    MissingGreenlet rather than the database error the test wanted. One forbidden
    statement per test, and no reuse of a session after it raises.
    """
    job_id, _rid = await _job_and_result(db, business_user)
    action = await _action(db, business_user, job_id)
    # Worker-written, i.e. no tenant GUC.
    await db.execute(text(
        "INSERT INTO contact_lookup_action_events (id, action_id, user_id, to_status) "
        "VALUES (:id, :a, :uid, 'running')"
    ), {"id": str(uuid.uuid4()), "a": action, "uid": business_user.id})
    await db.commit()

    await db.execute(text("SELECT set_config('app.current_user_id', :uid, true)"),
                     {"uid": str(business_user.id)})
    with pytest.raises(Exception) as exc:
        await db.execute(text(
            "UPDATE contact_lookup_action_events SET to_status = 'settled' "
            "WHERE action_id = :a"
        ), {"a": action})
    assert "append-only" in str(exc.value)
    await db.rollback()


async def test_the_migration_replays_after_a_half_applied_run(db):
    """The autocommit block COMMITS everything before it, while alembic_version is
    written only at the very end. So a lock timeout on the constraint attach
    leaves the columns durable and the revision unrecorded, and the next boot
    replays from the top.

    A plain ADD COLUMN aborts there on "column already exists" and the migration
    is stuck until a human intervenes. This asserts the statements that run
    BEFORE the autocommit block are individually replay-safe, which is the
    property that makes that recovery automatic. (Codex found this; an earlier
    version of this migration was not replay-safe and the proof was a mutation:
    reverting the IF NOT EXISTS reproduces the abort exactly.)
    """
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" /         "101_contact_lookup_action_schema.py"
    src = path.read_text(encoding="utf-8")
    pre = src[src.index("# ── 1. Additive columns"):src.index("# ── 2. Parent composite")]
    assert "ADD COLUMN IF NOT EXISTS last_trace_outcome" in pre
    assert "ADD COLUMN IF NOT EXISTS action_id" in pre
    # The CHECK cannot use IF NOT EXISTS, so it must carry a catalog guard.
    assert "IF NOT EXISTS (" in pre and "ck_results_last_trace_outcome" in pre
    # And nothing in that section may be a bare, unguarded ADD.
    assert "op.add_column(" not in pre,         "op.add_column has no IF NOT EXISTS: it would abort on replay"
