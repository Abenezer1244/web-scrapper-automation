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
from datetime import UTC, datetime

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


@pytest.mark.parametrize("status", ["running", "claimed", "settled", "failed"])
async def test_the_api_cannot_backdate_an_event_onto_a_moved_action(db, business_user, status):
    """Shape alone is not enough: the ACTION must still be at the hop the event
    claims to record.

    Otherwise a user-scoped session can append a fabricated "initial" event to an
    action that is already running or settled -- history a billing dispute would
    be argued from. A retry while the action is genuinely still `dispatching`
    stays legal, which the test above covers.
    """
    job_id, _rid = await _job_and_result(db, business_user)
    action = await _action(db, business_user, job_id)
    # Move it on, the way the worker would (no tenant GUC).
    await db.execute(text(
        "UPDATE contact_lookup_actions SET status = :s WHERE id = :a"
    ), {"s": status, "a": action})
    await db.commit()

    await db.execute(text("SELECT set_config('app.current_user_id', :uid, true)"),
                     {"uid": str(business_user.id)})
    with pytest.raises(Exception) as exc:
        await db.execute(text(
            "INSERT INTO contact_lookup_action_events (id, action_id, user_id, to_status) "
            "VALUES (:id, :a, :uid, 'dispatching')"
        ), {"id": str(uuid.uuid4()), "a": action, "uid": business_user.id})
    assert "still dispatching" in str(exc.value)
    await db.rollback()


async def test_the_api_cannot_write_a_fencing_lease_into_history(db, business_user):
    """`lease_token` fences a stalled worker against the reconciler. A value the
    request path invented has no business appearing in the record of it."""
    job_id, _rid = await _job_and_result(db, business_user)
    action = await _action(db, business_user, job_id)
    await db.execute(text("SELECT set_config('app.current_user_id', :uid, true)"),
                     {"uid": str(business_user.id)})
    with pytest.raises(Exception) as exc:
        await db.execute(text(
            "INSERT INTO contact_lookup_action_events "
            "(id, action_id, user_id, to_status, lease_token) "
            "VALUES (:id, :a, :uid, 'dispatching', 'forged-token')"
        ), {"id": str(uuid.uuid4()), "a": action, "uid": business_user.id})
    assert "lease" in str(exc.value).lower()
    await db.rollback()


# ─── Codex round 19: the ACTION row itself, and the races ────────────────────
# Until round 19 only verdicts and events were guarded. The API's INSERT on the
# action is table-wide, so it could create an action already `claimed`, holding
# a lease and invented counts; its UPDATE could resurrect a failed action. The
# API owns one hop (15-2): create in `dispatching`, then stamp `dispatched_at`
# once. These tests run as the table owner, which grants cannot restrain, so
# every refusal below is the TRIGGER's.

_FORGED = datetime(2099, 1, 1, tzinfo=UTC)


async def _as_api(db, user: User) -> None:
    await db.execute(text("SELECT set_config('app.current_user_id', :uid, true)"),
                     {"uid": str(user.id)})


async def test_the_api_may_create_an_action_and_the_server_owns_its_clock(db, business_user):
    """The permitted half, and the timestamp half of the fix.

    A server_default is skipped whenever the caller supplies a value, so an
    API-written future `created_at` would keep a `dispatching` action from ever
    expiring and strand its quoted rows. The trigger overwrites it."""
    job_id, _rid = await _job_and_result(db, business_user)
    await _as_api(db, business_user)
    aid = str(uuid.uuid4())
    await db.execute(text(
        "INSERT INTO contact_lookup_actions "
        "(id, user_id, job_id, category, quote_id, status, unit_price_cents, "
        " currency, pricing_version, quoted_count, truncated, created_at, "
        " status_changed_at) "
        "VALUES (:id, :uid, :job, 'tab', :q, 'dispatching', 8, 'usd', 'v1', "
        "        12, true, :f, :f)"
    ), {"id": aid, "uid": business_user.id, "job": job_id, "q": f"q-{aid}", "f": _FORGED})
    row = (await db.execute(text(
        "SELECT quoted_count, truncated, created_at < '2090-01-01' AS created_ok, "
        "       status_changed_at < '2090-01-01' AS changed_ok "
        "FROM contact_lookup_actions WHERE id = :a"
    ), {"a": aid})).one()
    assert (row.quoted_count, row.truncated) == (12, True), "the API's own snapshot is kept"
    assert row.created_ok, "created_at came from the request, not the server"
    assert row.changed_ok, "status_changed_at came from the request, not the server"
    await db.rollback()


@pytest.mark.parametrize("col,val", [
    ("status", "'claimed'"),
    ("status", "'running'"),
    ("lease_token", "'forged-lease'"),
    ("lease_expires_at", "now()"),
    ("started_at", "now()"),
    ("claimed_at", "now()"),
    ("settled_at", "now()"),
    ("dispatched_at", "now()"),
    ("status_reason", "'because I said so'"),
    ("claimed_count", "3"),
    ("reused_count", "3"),
    ("newly_queued_count", "3"),
    ("billable_rows", "3"),
    ("tracerfy_credits", "3"),
])
async def test_the_api_cannot_create_an_action_past_its_first_hop(db, business_user, col, val):
    """Each column here is a worker fact: a state it reached, a lease it holds,
    or a count billing reconciliation will read. The API may not assert any of
    them at creation."""
    job_id, _rid = await _job_and_result(db, business_user)
    await _as_api(db, business_user)
    aid = str(uuid.uuid4())
    # `status` is already in the base column list, so replace it rather than add it.
    status = val if col == "status" else "'dispatching'"
    extra_col = "" if col == "status" else f", {col}"
    extra_val = "" if col == "status" else f", {val}"
    with pytest.raises(Exception) as exc:
        await db.execute(text(
            "INSERT INTO contact_lookup_actions "
            "(id, user_id, job_id, category, quote_id, status, unit_price_cents, "
            f" currency, pricing_version{extra_col}) "
            f"VALUES (:id, :uid, :job, 'tab', :q, {status}, 8, 'usd', 'v1'{extra_val})"
        ), {"id": aid, "uid": business_user.id, "job": job_id, "q": f"q-{aid}"})
    assert "initial dispatching state" in str(exc.value), col
    await db.rollback()


async def test_the_api_stamps_dispatched_at_once_with_the_servers_clock(db, business_user):
    job_id, _rid = await _job_and_result(db, business_user)
    action = await _action(db, business_user, job_id)
    await _as_api(db, business_user)
    await db.execute(text(
        "UPDATE contact_lookup_actions SET dispatched_at = :f "
        "WHERE id = :a"
    ), {"f": _FORGED, "a": action})
    ok = (await db.execute(text(
        "SELECT dispatched_at < '2090-01-01' FROM contact_lookup_actions WHERE id = :a"
    ), {"a": action})).scalar_one()
    assert ok, "dispatched_at came from the request, not the server"
    await db.rollback()


@pytest.mark.parametrize("worker_set,api_set,why", [
    ("status = 'dispatching'", "status = 'settled'", "settling a live action"),
    ("status = 'failed'", "status = 'dispatching'", "resurrecting a failed action"),
    ("status = 'expired'", "dispatched_at = now()", "reviving an expired one by dispatching it"),
    ("status = 'running'", "dispatched_at = now()", "stamping an action the worker already owns"),
    ("dispatched_at = now()", "dispatched_at = now()", "restamping the dispatch clock"),
    ("status = 'dispatching'", "quoted_count = 9999", "rewriting the quoted snapshot"),
    ("status = 'dispatching'", "billable_rows = 7", "inventing a charge"),
    ("status = 'dispatching'", "dispatched_at = now(), status_reason = 'x'",
     "smuggling a second column beside the permitted one"),
    ("status = 'dispatching'", "dispatched_at = now(), quoted_count = 9999",
     "rewriting the quoted snapshot under cover of a legitimate stamp"),
    # A real change: billable_rows is 0 already, so "= 0" would be a no-op the
    # guard rightly allows.
    ("status = 'dispatching'", "dispatched_at = now(), billable_rows = 7",
     "inventing a charge under cover of a legitimate stamp"),
])
async def test_the_api_cannot_move_an_action(db, business_user, worker_set, api_set, why):
    job_id, _rid = await _job_and_result(db, business_user)
    action = await _action(db, business_user, job_id)
    # Arrange as the worker does: no tenant GUC.
    await db.execute(text(f"UPDATE contact_lookup_actions SET {worker_set} WHERE id = :a"),
                     {"a": action})
    await db.commit()
    await _as_api(db, business_user)
    with pytest.raises(Exception) as exc:
        await db.execute(text(f"UPDATE contact_lookup_actions SET {api_set} WHERE id = :a"),
                         {"a": action})
    assert "may only stamp dispatched_at" in str(exc.value), why
    await db.rollback()


async def test_the_api_cannot_delete_an_action(db, business_user):
    """One forbidden statement, on a session nothing else holds a lock for (see
    the 78-minute note on the event-rewrite test above)."""
    job_id, _rid = await _job_and_result(db, business_user)
    action = await _action(db, business_user, job_id)
    await _as_api(db, business_user)
    with pytest.raises(Exception) as exc:
        await db.execute(text("DELETE FROM contact_lookup_actions WHERE id = :a"),
                         {"a": action})
    assert "may not delete an action" in str(exc.value)
    await db.rollback()


async def test_the_worker_may_move_an_action_anywhere(db, business_user):
    """The guard keys on the tenant GUC. If this ever fails, the action worker is
    using rls_sync_session() instead of system_sync_session()."""
    job_id, _rid = await _job_and_result(db, business_user)
    action = await _action(db, business_user, job_id)
    await db.execute(text(
        "UPDATE contact_lookup_actions SET status = 'claimed', lease_token = 't', "
        "claimed_count = 5, billable_rows = 5 WHERE id = :a"
    ), {"a": action})
    got = (await db.execute(text(
        "SELECT status FROM contact_lookup_actions WHERE id = :a"
    ), {"a": action})).scalar_one()
    assert got == "claimed"
    await db.rollback()


_NO_TENANT_INSERTS = {
    "contact_lookup_actions": (
        "INSERT INTO contact_lookup_actions "
        "(id, user_id, job_id, category, quote_id, status, unit_price_cents, "
        " currency, pricing_version) "
        "VALUES (:id, :uid, :job, 'tab', :q, 'claimed', 8, 'usd', 'v1')"
    ),
    "contact_lookup_action_results": (
        "INSERT INTO contact_lookup_action_results "
        "(id, action_id, user_id, result_id, disposition) "
        "VALUES (:id, :id, :uid, :id, 'newly_queued')"
    ),
    "contact_lookup_action_events": (
        "INSERT INTO contact_lookup_action_events "
        "(id, action_id, user_id, from_status, to_status) "
        "VALUES (:id, :id, :uid, 'running', 'settled')"
    ),
}


@pytest.mark.parametrize("table", sorted(_NO_TENANT_INSERTS))
async def test_the_api_role_without_a_tenant_is_not_the_worker(db, business_user, table):
    """Codex round 20: an empty GUC used to mean "the worker", whatever the role.
    In production the API's sync DSN logs in as bridgeleads_app too, so an API
    code path that opened a system session would have passed every guard as the
    worker. Run as the real API role, with no tenant set, and write a worker
    verdict: the guard itself must refuse it."""
    job_id, _rid = await _job_and_result(db, business_user)
    await db.execute(text("SET LOCAL ROLE bridgeleads_app"))
    with pytest.raises(Exception) as exc:
        await db.execute(text(_NO_TENANT_INSERTS[table]), {
            "id": str(uuid.uuid4()), "q": f"q-{uuid.uuid4()}",
            "uid": business_user.id, "job": job_id,
        })
    assert "not the worker role" in str(exc.value)
    assert table in str(exc.value)
    await db.rollback()


async def test_the_worker_role_without_a_tenant_passes_the_guard(db, business_user):
    """The other side: bridgeleads_system with no GUC is the worker, and the
    guard lets it through. The test DB carries only the migration's tenant
    policy (the role-targeted _system policies come from the cutover script),
    so the row is then refused by RLS. That refusal is the proof the TRIGGER
    passed it: the guard would have raised first, with its own message."""
    job_id, _rid = await _job_and_result(db, business_user)
    await db.execute(text("SET LOCAL ROLE bridgeleads_system"))
    with pytest.raises(Exception) as exc:
        await db.execute(text(_NO_TENANT_INSERTS["contact_lookup_actions"]), {
            "id": str(uuid.uuid4()), "q": f"q-{uuid.uuid4()}",
            "uid": business_user.id, "job": job_id,
        })
    assert "not the worker role" not in str(exc.value)
    assert "row-level security" in str(exc.value)
    await db.rollback()


async def test_quoted_count_cannot_be_negative(db, business_user):
    job_id, _rid = await _job_and_result(db, business_user)
    with pytest.raises(Exception) as exc:
        await db.execute(text(
            "INSERT INTO contact_lookup_actions "
            "(id, user_id, job_id, category, quote_id, status, unit_price_cents, "
            " currency, pricing_version, quoted_count) "
            "VALUES (:id, :uid, :job, 'tab', :q, 'dispatching', 8, 'usd', 'v1', -1)"
        ), {"id": str(uuid.uuid4()), "uid": business_user.id, "job": job_id,
            "q": f"q-{uuid.uuid4()}"})
    assert "ck_contact_lookup_actions_quoted_count" in str(exc.value)
    await db.rollback()


async def test_the_api_event_carries_no_reason(db, business_user):
    job_id, _rid = await _job_and_result(db, business_user)
    action = await _action(db, business_user, job_id)
    await _as_api(db, business_user)
    with pytest.raises(Exception) as exc:
        await db.execute(text(
            "INSERT INTO contact_lookup_action_events "
            "(id, action_id, user_id, to_status, reason) "
            "VALUES (:id, :a, :uid, 'dispatching', 'worker said it was fine')"
        ), {"id": str(uuid.uuid4()), "a": action, "uid": business_user.id})
    assert "initial dispatching event" in str(exc.value)
    await db.rollback()


async def test_a_temp_table_cannot_stand_in_for_the_action(db, business_user):
    """Codex round 20: the guard's parent read was unqualified, so a session's
    own pg_temp.contact_lookup_actions, saying `dispatching`, could vouch for a
    real action the worker had already moved on, and the API would write a
    fabricated initial event into history after the fact."""
    job_id, _rid = await _job_and_result(db, business_user)
    action = await _action(db, business_user, job_id)
    await db.execute(text(
        "UPDATE contact_lookup_actions SET status = 'running' WHERE id = :a"
    ), {"a": action})
    await db.commit()

    await _as_api(db, business_user)
    await db.execute(text(
        "CREATE TEMP TABLE contact_lookup_actions "
        "(LIKE public.contact_lookup_actions) ON COMMIT DROP"
    ))
    await db.execute(text(
        "INSERT INTO pg_temp.contact_lookup_actions "
        "SELECT * FROM public.contact_lookup_actions WHERE id = :a"
    ), {"a": action})
    await db.execute(text(
        "UPDATE pg_temp.contact_lookup_actions SET status = 'dispatching'"
    ))
    with pytest.raises(Exception) as exc:
        await db.execute(text(
            "INSERT INTO public.contact_lookup_action_events "
            "(id, action_id, user_id, to_status) VALUES (:id, :a, :uid, 'dispatching')"
        ), {"id": str(uuid.uuid4()), "a": action, "uid": business_user.id})
    assert "still dispatching" in str(exc.value)
    await db.rollback()


async def test_api_written_history_carries_the_servers_clock(db, business_user):
    """Event `at` and verdict `decided_at` are what a billing dispute is dated by."""
    job_id, rid = await _job_and_result(db, business_user)
    action = await _action(db, business_user, job_id)
    await _as_api(db, business_user)
    ev, vd = str(uuid.uuid4()), str(uuid.uuid4())
    await db.execute(text(
        "INSERT INTO contact_lookup_action_events (id, action_id, user_id, to_status, at) "
        "VALUES (:id, :a, :uid, 'dispatching', :f)"
    ), {"id": ev, "a": action, "uid": business_user.id, "f": _FORGED})
    await db.execute(text(
        "INSERT INTO contact_lookup_action_results "
        "(id, action_id, user_id, result_id, disposition, decided_at) "
        "VALUES (:id, :a, :uid, :r, 'quoted', :f)"
    ), {"id": vd, "a": action, "uid": business_user.id, "r": rid, "f": _FORGED})
    at_ok = (await db.execute(text(
        "SELECT at < '2090-01-01' FROM contact_lookup_action_events WHERE id = :i"
    ), {"i": ev})).scalar_one()
    decided_ok = (await db.execute(text(
        "SELECT decided_at < '2090-01-01' FROM contact_lookup_action_results WHERE id = :i"
    ), {"i": vd})).scalar_one()
    assert at_ok, "event `at` came from the request"
    assert decided_ok, "verdict `decided_at` came from the request"
    await db.rollback()


async def _race_against_the_worker(user: User, api_sql: str, params: dict):
    """Hold the worker's `dispatching -> running` UPDATE open in one session,
    fire the API statement from another, then commit the worker.

    Returns (blocked, error): whether the API statement was still waiting while
    the worker held its row lock, and what it raised once the worker committed.
    Bounded twice (lock_timeout and wait_for) so a regression fails in seconds
    instead of hanging the run.
    """
    import asyncio

    from src.db import session as _db_session

    worker = _db_session.AsyncSessionLocal()
    api = _db_session.AsyncSessionLocal()
    try:
        await worker.execute(text(
            "UPDATE contact_lookup_actions SET status = 'running' WHERE id = :a"
        ), {"a": params["a"]})

        async def fire():
            await api.execute(text("SET LOCAL lock_timeout = '10s'"))
            await _as_api(api, user)
            await api.execute(text(api_sql), params)

        task = asyncio.create_task(fire())
        await asyncio.sleep(1.0)
        blocked = not task.done()
        await worker.commit()
        error = None
        try:
            await asyncio.wait_for(task, timeout=15)
        except Exception as e:  # the refusal under test
            error = e
        return blocked, error
    finally:
        await api.rollback()
        await api.close()
        await worker.rollback()
        await worker.close()


async def test_the_event_guard_waits_for_the_worker_and_then_refuses(db, business_user):
    """19-2. Without FOR SHARE the API reads `dispatching`, the worker commits
    `running`, and a `dispatching` event lands in history AFTER it. With it, the
    API waits on the worker's row lock and re-reads the committed row."""
    job_id, _rid = await _job_and_result(db, business_user)
    action = await _action(db, business_user, job_id)
    blocked, error = await _race_against_the_worker(
        business_user,
        "INSERT INTO contact_lookup_action_events (id, action_id, user_id, to_status) "
        "VALUES (:id, :a, :uid, 'dispatching')",
        {"id": str(uuid.uuid4()), "a": action, "uid": business_user.id},
    )
    assert blocked, "the API's event insert did not wait for the worker's row lock"
    assert error is not None and "still dispatching" in str(error), error


async def test_the_api_dispatch_stamp_re_reads_a_row_the_worker_moved(db, business_user):
    """19-1's race. A BEFORE UPDATE trigger fires on the row version the UPDATE
    locked, so the API's stamp is judged against `running`, not the stale
    `dispatching` it first saw."""
    job_id, _rid = await _job_and_result(db, business_user)
    action = await _action(db, business_user, job_id)
    blocked, error = await _race_against_the_worker(
        business_user,
        "UPDATE contact_lookup_actions SET dispatched_at = now() WHERE id = :a",
        {"a": action},
    )
    assert blocked, "the API's update did not wait for the worker's row lock"
    assert error is not None and "may only stamp dispatched_at" in str(error), error
