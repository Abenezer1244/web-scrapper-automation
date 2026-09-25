"""The spend ledger's weight becomes a database fact (102).

Phase 1b-1b-ii-a, ahead of the credit-weighted dispatch cap. The cap charges a
row by its trace_type (normal = 1 Tracerfy credit, advanced = 2) over a rolling
24h window of `submitted_at`. Two things had to become true in the database
rather than in whoever remembers them (Codex pre-code consult R4, R6, R7, R8):

  1. A spent row's WEIGHT cannot be rewritten. 1b-1b-i made "trace_type never
     changes once a row is spent" an application invariant only; a hand edit or
     a future script could turn a spent advanced row into a normal one and hand
     that account a credit it never had. The trigger below refuses a trace_type
     change whenever the OLD or the NEW row carries submission evidence
     (submitted_at or tracerfy_queue_id), which also covers a single UPDATE that
     sets the type and the evidence together. It is plain plpgsql, not SECURITY
     DEFINER, and fires for every role: the worker has no reason to do this
     either. Rows that were never sent may still change type; the probate
     repair's name refresh does exactly that, on queued rows with no evidence.
  2. Only known types exist. An unknown trace_type would otherwise have to be
     guessed at (the old weight lookup defaulted to 1, undercounting). The CHECK
     is added NOT VALID and then validated, and a pre-check aborts with the
     offending count first, so the failure says what to do.

And the cap's spent query (sum of weight by user over the window) gets a
partial index shaped for it: (submitted_at) INCLUDE (user_id, trace_type) WHERE
submitted_at IS NOT NULL. Built CONCURRENTLY with 100's identity check, so a
same-named index of the wrong shape is rebuilt rather than trusted. It is also
listed in alembic/env.py CONCURRENT_INDEXES so autogenerate never proposes a
blocking plain build of it.

RESTART SAFETY (the 101 lesson): autocommit_block() COMMITS the transaction it
is entered from, so the index is built FIRST, before any transactional DDL, and
every statement after it is idempotent (constraint by catalog lookup, function
by CREATE OR REPLACE, trigger by DROP IF EXISTS + CREATE). A lock timeout at any
step leaves a database this migration can simply run again. Dropping an INVALID
index corpse is safe only because migrations are serialized by the advisory
lock in scripts/migrate.py, which start.sh boots through.

Backward safe: the running code never changes a spent row's type and only ever
writes 'normal' or 'advanced', so a rollback of the code with 102 applied is
harmless.

Revision ID: 102
Revises: 101
Create Date: 2026-09-25
"""
from alembic import op
from sqlalchemy import text

revision = "102"
down_revision = "101"
branch_labels = None
depends_on = None

_INDEX = "ix_pending_skip_trace_spent"
_CHECK = "ck_pending_skip_trace_rows_trace_type"
_FN = "pending_skip_trace_rows_weight_guard"
_TRIGGER = "pending_skip_trace_rows_weight_guard_trg"

_GUARD_FN = f"""
CREATE OR REPLACE FUNCTION {_FN}()
RETURNS trigger AS $fn$
BEGIN
    IF NEW.trace_type IS DISTINCT FROM OLD.trace_type
       AND (OLD.submitted_at IS NOT NULL OR OLD.tracerfy_queue_id IS NOT NULL
            OR NEW.submitted_at IS NOT NULL OR NEW.tracerfy_queue_id IS NOT NULL) THEN
        RAISE EXCEPTION
            'pending_skip_trace_rows: trace_type of a spent row cannot change '
            '(row %, % -> %). Its weight is what the daily credit cap charged; '
            'rewriting it would hand the account credits it never had.',
            OLD.id, OLD.trace_type, NEW.trace_type
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$fn$ LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp;
"""


def _build_spent_index(conn) -> None:
    """CREATE INDEX CONCURRENTLY, checked by identity (the 100 pattern)."""
    existing = conn.execute(text(
        "SELECT i.indisvalid, i.indisunique, i.indnkeyatts, i.indnatts, "
        "       i.indexprs IS NULL AS plain_columns, "
        "       pg_get_expr(i.indpred, i.indrelid) AS predicate, "
        "       ARRAY(SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY k(n, o) "
        "             JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.n "
        "             ORDER BY k.o) AS cols "
        "FROM pg_class c "
        "JOIN pg_namespace cn ON cn.oid = c.relnamespace "
        "JOIN pg_index i ON i.indexrelid = c.oid "
        "JOIN pg_class t ON t.oid = i.indrelid "
        "JOIN pg_namespace tn ON tn.oid = t.relnamespace "
        "WHERE c.relname = :n AND cn.nspname = 'public' "
        "  AND t.relname = 'pending_skip_trace_rows' AND tn.nspname = 'public'"
    ), {"n": _INDEX}).first()
    right_shape = existing is not None and (
        existing.indisvalid
        and not existing.indisunique
        and existing.plain_columns
        and existing.indnkeyatts == 1
        and existing.indnatts == 3
        and list(existing.cols) == ["submitted_at", "user_id", "trace_type"]
        and " ".join((existing.predicate or "").split()) == "(submitted_at IS NOT NULL)"
    )
    if existing is not None and not right_shape:
        # Dead (INVALID) or the wrong shape. Safe to drop here: migrations are
        # serialized by scripts/migrate.py's advisory lock, so this is never a
        # build still in progress.
        conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS public.{_INDEX}"))
    elif existing is None:
        collision = conn.execute(text(
            "SELECT tn.nspname || '.' || t.relname FROM pg_class c "
            "JOIN pg_namespace cn ON cn.oid = c.relnamespace "
            "JOIN pg_index i ON i.indexrelid = c.oid "
            "JOIN pg_class t ON t.oid = i.indrelid "
            "JOIN pg_namespace tn ON tn.oid = t.relnamespace "
            "WHERE c.relname = :n AND cn.nspname = 'public'"
        ), {"n": _INDEX}).scalar()
        if collision:
            raise RuntimeError(
                f"Migration 102 ABORTED: an index named {_INDEX} already exists on "
                f"{collision}, which is not public.pending_skip_trace_rows. It is NOT "
                f"dropped, because it may be something else's. Rename or remove it "
                f"deliberately, then re-run."
            )
    if not right_shape:
        conn.execute(text(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
            f"ON public.pending_skip_trace_rows (submitted_at) "
            f"INCLUDE (user_id, trace_type) WHERE submitted_at IS NOT NULL"
        ))


def upgrade() -> None:
    conn = op.get_bind()

    # Guard first, so an unknown type produces an instruction rather than a bare
    # "check constraint is violated by some row" from VALIDATE.
    unknown = conn.execute(text(
        "SELECT count(*), count(*) FILTER (WHERE submitted_at IS NOT NULL "
        "                                    OR tracerfy_queue_id IS NOT NULL) "
        "FROM public.pending_skip_trace_rows "
        "WHERE trace_type NOT IN ('normal', 'advanced')"
    )).one()
    if unknown[0]:
        raise RuntimeError(
            f"Migration 102 ABORTED: {unknown[0]} pending_skip_trace_rows carry a "
            f"trace_type other than 'normal' or 'advanced' ({unknown[1]} of them with "
            f"submission evidence). The daily credit cap cannot weigh them. Find out "
            f"what each one was sent as (its Tracerfy queue's trace_type) and correct "
            f"it before re-running; do not guess."
        )

    # The index first: autocommit_block commits everything before it (101).
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        conn.execute(text("SET lock_timeout = '5s'"))
        try:
            _build_spent_index(conn)
        finally:
            conn.execute(text("RESET lock_timeout"))

    conn = op.get_bind()
    conn.execute(text("SET LOCAL lock_timeout = '5s'"))
    has_check = conn.execute(text(
        "SELECT 1 FROM pg_constraint WHERE conname = :c "
        "AND conrelid = 'public.pending_skip_trace_rows'::regclass"
    ), {"c": _CHECK}).first()
    if not has_check:
        # NOT VALID: the ADD takes its brief lock without scanning the table.
        conn.execute(text(
            f"ALTER TABLE public.pending_skip_trace_rows ADD CONSTRAINT {_CHECK} "
            f"CHECK (trace_type IN ('normal', 'advanced')) NOT VALID"
        ))
    # VALIDATE takes only SHARE UPDATE EXCLUSIVE, and is a no-op once valid.
    conn.execute(text(
        f"ALTER TABLE public.pending_skip_trace_rows VALIDATE CONSTRAINT {_CHECK}"
    ))
    conn.execute(text(_GUARD_FN))
    conn.execute(text(
        f"DROP TRIGGER IF EXISTS {_TRIGGER} ON public.pending_skip_trace_rows"
    ))
    # OF trace_type: the dispatcher's frequent status updates never pay for it.
    conn.execute(text(
        f"CREATE TRIGGER {_TRIGGER} BEFORE UPDATE OF trace_type "
        f"ON public.pending_skip_trace_rows FOR EACH ROW EXECUTE FUNCTION {_FN}()"
    ))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("SET LOCAL lock_timeout = '5s'"))
    conn.execute(text(
        f"DROP TRIGGER IF EXISTS {_TRIGGER} ON public.pending_skip_trace_rows"
    ))
    conn.execute(text(f"DROP FUNCTION IF EXISTS {_FN}()"))
    conn.execute(text(
        f"ALTER TABLE public.pending_skip_trace_rows DROP CONSTRAINT IF EXISTS {_CHECK}"
    ))
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        conn.execute(text("SET lock_timeout = '5s'"))
        try:
            conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS public.{_INDEX}"))
        finally:
            conn.execute(text("RESET lock_timeout"))
