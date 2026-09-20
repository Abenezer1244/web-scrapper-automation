"""One active skip-trace claim per lead (099).

Nothing stopped two pending_skip_trace_rows existing for one result_id in an
active status. The scrape enqueue got away with it because it is the only writer
and it filters on ``results.skip_trace_status = 'not_attempted'``. Phase 1b adds
a SECOND writer -- the "look up contacts" action -- and the two race: the action
and a scrape can both decide a lead is eligible, and both insert. The customer
is then charged twice for one lead and the vendor is asked the same question
twice.

The partial unique index below is what makes that impossible in the database
rather than in whoever remembers to check. It is the arbiter for the
``INSERT ... ON CONFLICT (result_id) WHERE status IN (...) DO NOTHING``
in src/workers/skip_trace_claim.py, so its predicate and ACTIVE_PENDING_STATUSES
there must stay identical: widen one without the other and either a second claim
slips through (double charge) or a legitimate claim is refused forever.

PREREQUISITE, ENFORCED NOT ASSUMED (Codex round 15, finding 15-1). The existing
scrape enqueue had to move onto that shared ON CONFLICT claim FIRST. It used to
build rows with db.add() and flush them at one commit() guarded by
``except Exception: db.rollback(); db.commit()``; with this index in place, a
single conflict would have rolled back an entire job's enqueue -- every pending
row and every results status update -- and committed an empty transaction, with
nothing logged and the leads left silently un-traced.

Verified read-only against production 2026-09-20 before writing this: 0 groups
with more than one active row, 0 drift between pending status and
results.skip_trace_status, so the index is creatable as-is and NO cleanup script
is needed. That was a point-in-time read and this migration runs later, so the
guard below re-checks and ABORTS with instructions rather than failing on a raw
unique violation. If it ever fires, the duplicates must be graded by SUBMISSION
EVIDENCE, not by age (15-7): a ``tracerfy_queue_id`` is proof the vendor accepted
and charged; ``status='submitting'`` with no queue id is an UNKNOWN provider
outcome; ``submitted_at`` alone proves only a local attempt, because the
dispatcher stamps it at queued -> submitting BEFORE it contacts Tracerfy
(skip_trace_dispatcher.py:280-289). Cancelling the wrong one buys a second paid
submission, so any group containing an unknown-outcome row is quarantined whole
and reconciled against Tracerfy by hand.

Lock safety: the index is built CONCURRENTLY in an autocommit block, so it never
blocks reads or writes on a table the dispatcher is draining. Every step is
idempotent because the autocommit block leaves the revision unrecorded if the
build times out (the 098 restart-safety pattern).

Revision ID: 099
Revises: 098
Create Date: 2026-09-20
"""
from alembic import op
from sqlalchemy import text

revision = "099"
down_revision = "098"
branch_labels = None
depends_on = None

_INDEX = "uq_pending_skip_trace_active_result"
_ACTIVE = "('queued','submitting','submitted')"

# How Postgres renders the WHERE clause below back from the catalog. Duplicated
# deliberately from src/workers/skip_trace_claim.py rather than imported: a
# migration must keep working when the application code moves on, and the
# runtime check there asserts the same thing independently, so a drift between
# the two surfaces as a refusal to claim rather than as a silent no-op.
_EXPECTED_PREDICATE = (
    "((status)::text = ANY ((ARRAY['queued'::character varying, "
    "'submitting'::character varying, 'submitted'::character varying])::text[]))"
)


def upgrade() -> None:
    conn = op.get_bind()

    # Guard BEFORE the build, so a duplicate produces this instruction rather
    # than a bare "could not create unique index" with a tuple in it.
    duplicates = conn.execute(text(
        f"SELECT count(*) FROM (SELECT result_id FROM public.pending_skip_trace_rows "
        f"WHERE status IN {_ACTIVE} GROUP BY result_id HAVING count(*) > 1) t"
    )).scalar()
    if duplicates:
        detail = conn.execute(text(
            f"SELECT count(*) FILTER (WHERE n_evidence > 0) AS with_evidence "
            f"FROM (SELECT result_id, count(*) FILTER ("
            f"        WHERE tracerfy_queue_id IS NOT NULL OR status = 'submitting'"
            f"      ) AS n_evidence "
            f"      FROM public.pending_skip_trace_rows WHERE status IN {_ACTIVE} "
            f"      GROUP BY result_id HAVING count(*) > 1) t"
        )).scalar()
        raise RuntimeError(
            f"Migration 099 ABORTED: {duplicates} result_id(s) already have more than "
            f"one ACTIVE pending_skip_trace_row, {detail} of them containing a row "
            f"that may have reached Tracerfy. Do NOT resolve these by age. Grade each "
            f"group by submission evidence (tracerfy_queue_id = vendor accepted and "
            f"charged; status='submitting' with no queue id = UNKNOWN outcome; "
            f"submitted_at alone = local attempt only). Quarantine any group holding "
            f"an unknown-outcome row and reconcile it against Tracerfy before retrying. "
            f"See tasks/todo-lookup-contacts.md finding 15-7."
        )

    with op.get_context().autocommit_block():
        conn = op.get_bind()
        conn.execute(text("SET lock_timeout = '5s'"))
        try:
            # A failed CONCURRENTLY build leaves the index behind marked INVALID
            # and it stays invalid forever: IF NOT EXISTS would see the name,
            # skip the build, and the constraint would never actually hold while
            # appearing to have been applied. Drop that corpse before rebuilding.
            #
            # Safe here, unlike in a live-ops context, because migrations are
            # serialized by the advisory lock in scripts/migrate.py, so an
            # invalid index at this point is a dead one and never a build still
            # in progress. (Outside a migration, indisvalid=false means BUILDING
            # *or* DEAD -- check pg_stat_progress_create_index before declaring
            # failure.)
            #
            # The check is by IDENTITY, not just validity. `CREATE ... IF NOT
            # EXISTS` treats ANY same-named index as success, so a non-unique
            # one, a composite one, one on another schema's table, or one with
            # an extra predicate conjunct would leave 099 recorded as applied
            # while the money-safety constraint it exists for is absent.
            existing = conn.execute(text(
                "SELECT i.indisvalid, i.indisunique, i.indnatts, i.indnkeyatts, "
                "       i.indexprs IS NULL AS plain_columns, a.attname, "
                "       pg_get_expr(i.indpred, i.indrelid) AS predicate "
                "FROM pg_class c "
                "JOIN pg_namespace cn ON cn.oid = c.relnamespace "
                "JOIN pg_index i ON i.indexrelid = c.oid "
                "JOIN pg_class t ON t.oid = i.indrelid "
                "JOIN pg_namespace tn ON tn.oid = t.relnamespace "
                "LEFT JOIN pg_attribute a "
                "       ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] "
                "WHERE c.relname = :n AND cn.nspname = 'public' "
                "  AND t.relname = 'pending_skip_trace_rows' AND tn.nspname = 'public'"
            ), {"n": _INDEX}).first()
            # Structural, not textual: matching "(result_id)" in the index
            # definition would also accept an expression index, which cannot
            # serve the named ON CONFLICT arbiter and would fail every claim.
            wrong_shape = existing is not None and not (
                existing.indisvalid
                and existing.indisunique
                # indnkeyatts counts KEY columns; indnatts also counts INCLUDE
                # payload columns. ON CONFLICT infers on the key alone, so an
                # index with INCLUDE columns still arbitrates and must not be
                # rebuilt out from under a database that has one.
                and existing.indnkeyatts == 1
                and existing.indnatts >= existing.indnkeyatts
                and existing.plain_columns
                and existing.attname == "result_id"
                and " ".join((existing.predicate or "").split())
                == " ".join(_EXPECTED_PREDICATE.split())
            )
            if wrong_shape:
                conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS public.{_INDEX}"))
            elif existing is None:
                # Right name, but NOT on public.pending_skip_trace_rows. Dropping
                # it would destroy an unrelated index that something else needs,
                # and an unqualified CREATE could then resolve against whatever
                # search_path points at. Refuse and let a human look.
                # Restricted to the PUBLIC schema. Index names only need to be
                # unique within their own schema, so a same-named index in some
                # other schema is not a collision at all and aborting on it
                # would block the deploy for no reason.
                collision = conn.execute(text(
                    "SELECT tn.nspname || '.' || t.relname "
                    "FROM pg_class c "
                    "JOIN pg_namespace cn ON cn.oid = c.relnamespace "
                    "JOIN pg_index i ON i.indexrelid = c.oid "
                    "JOIN pg_class t ON t.oid = i.indrelid "
                    "JOIN pg_namespace tn ON tn.oid = t.relnamespace "
                    "WHERE c.relname = :n AND cn.nspname = 'public'"
                ), {"n": _INDEX}).scalar()
                if collision:
                    raise RuntimeError(
                        f"Migration 099 ABORTED: an index named {_INDEX} already "
                        f"exists on {collision}, which is not "
                        f"public.pending_skip_trace_rows. It is NOT dropped, "
                        f"because it may be something else's. Rename or remove it "
                        f"deliberately, then re-run."
                    )
            # Fully qualified: an unqualified name resolves against search_path,
            # which could create the index on a shadow table and record 099 as
            # applied while the real table stayed unenforced.
            conn.execute(text(
                f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
                f"ON public.pending_skip_trace_rows (result_id) "
                f"WHERE status IN {_ACTIVE}"
            ))
        finally:
            conn.execute(text("RESET lock_timeout"))


def downgrade() -> None:
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        conn.execute(text("SET lock_timeout = '5s'"))
        try:
            conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS public.{_INDEX}"))
        finally:
            conn.execute(text("RESET lock_timeout"))
