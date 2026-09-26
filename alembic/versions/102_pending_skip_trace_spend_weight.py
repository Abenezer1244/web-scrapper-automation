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
is entered from, so after the read-only guard EVERYTHING runs in autocommit, one
statement per transaction, and every statement is idempotent: the index and the
constraint by identity (the server's own rendering, compared whole; a
same-named impostor on the constraint aborts rather than being trusted), the
function and trigger by CREATE OR REPLACE. No table lock outlives its own
statement, and a lock timeout at any step leaves a database this migration can
simply run again. Dropping an INVALID
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
    RETURN NULL;
END;
$fn$ LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp;
"""

# AFTER, not BEFORE UPDATE OF trace_type (Codex ii-a review): an AFTER trigger
# sees the row as finally written, so a BEFORE trigger that rewrites
# NEW.trace_type, or a statement whose SET list omits the column, cannot slip
# past it. The WHEN clause is evaluated on that final row before the event is
# even queued, so the dispatcher's status updates still cost nothing.
_TRIGGER_DDL = (
    f"CREATE OR REPLACE TRIGGER {_TRIGGER} AFTER UPDATE "
    f"ON public.pending_skip_trace_rows FOR EACH ROW "
    f"WHEN (OLD.trace_type IS DISTINCT FROM NEW.trace_type) "
    f"EXECUTE FUNCTION {_FN}()"
)

# Identity is the server's own rendering of the object, compared whole: it
# carries the access method, key order, collation, operator class, INCLUDE list,
# predicate and uniqueness, so a same-named object that differs in ANY of them
# is caught (Codex ii-a review).
_INDEX_DEF = (
    f"CREATE INDEX {_INDEX} ON public.pending_skip_trace_rows USING btree "
    f"(submitted_at) INCLUDE (user_id, trace_type) WHERE (submitted_at IS NOT NULL)"
)
_CHECK_DEF = (
    "CHECK (((trace_type)::text = ANY ((ARRAY['normal'::character varying, "
    "'advanced'::character varying])::text[])))"
)


def _build_spent_index(conn) -> None:
    """CREATE INDEX CONCURRENTLY, checked by identity (the 100 pattern)."""
    existing = conn.execute(text(
        "SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) AS indexdef "
        "FROM pg_class c "
        "JOIN pg_namespace cn ON cn.oid = c.relnamespace "
        "JOIN pg_index i ON i.indexrelid = c.oid "
        "JOIN pg_class t ON t.oid = i.indrelid "
        "JOIN pg_namespace tn ON tn.oid = t.relnamespace "
        "WHERE c.relname = :n AND cn.nspname = 'public' "
        "  AND t.relname = 'pending_skip_trace_rows' AND tn.nspname = 'public'"
    ), {"n": _INDEX}).first()
    right_shape = existing is not None and existing.indisvalid and existing.indexdef == _INDEX_DEF
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


def _ensure_trace_type_check(conn) -> None:
    """ADD the CHECK NOT VALID if missing, refuse a same-named impostor, VALIDATE.

    Run in autocommit, so the ADD's ACCESS EXCLUSIVE lock is released as soon as
    that one catalog-only statement commits, and VALIDATE's full scan holds only
    SHARE UPDATE EXCLUSIVE, which does not block the dispatcher's writes
    (Codex ii-a review: a lock_timeout bounds the WAIT, not how long a lock is
    held once taken, so nothing may sit behind an open transaction here).
    """
    existing = conn.execute(text(
        "SELECT contype, pg_get_constraintdef(oid) AS condef FROM pg_constraint "
        "WHERE conname = :c AND conrelid = 'public.pending_skip_trace_rows'::regclass"
    ), {"c": _CHECK}).first()
    if existing is None:
        conn.execute(text(
            f"ALTER TABLE public.pending_skip_trace_rows ADD CONSTRAINT {_CHECK} "
            f"CHECK (trace_type IN ('normal', 'advanced')) NOT VALID"
        ))
    else:
        condef = existing.condef.removesuffix(" NOT VALID")
        if existing.contype != "c" or condef != _CHECK_DEF:
            # Not ours, or not what the cap relies on. Validating it would leave
            # the invariant absent under the right name, so stop and say why.
            raise RuntimeError(
                f"Migration 102 ABORTED: pending_skip_trace_rows already has a "
                f"constraint named {_CHECK} that is not the expected CHECK "
                f"(found type {existing.contype!r}: {existing.condef}). It is NOT "
                f"dropped, because it may be something else's. Remove or rename it "
                f"deliberately, then re-run."
            )
    # A no-op once valid.
    conn.execute(text(
        f"ALTER TABLE public.pending_skip_trace_rows VALIDATE CONSTRAINT {_CHECK}"
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

    # Everything below runs one statement per transaction. autocommit_block
    # commits the guard's transaction on entry (the 101 lesson), and every step
    # is idempotent, so a lock timeout anywhere leaves a database this migration
    # can simply run again.
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        conn.execute(text("SET lock_timeout = '5s'"))
        try:
            _build_spent_index(conn)
            _ensure_trace_type_check(conn)
            conn.execute(text(_GUARD_FN))
            conn.execute(text(_TRIGGER_DDL))
        finally:
            conn.execute(text("RESET lock_timeout"))


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
            # Only ours: a same-named index on another table is left alone.
            ours = conn.execute(text(
                "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relname = :n AND n.nspname = 'public' "
                "  AND i.indrelid = 'public.pending_skip_trace_rows'::regclass"
            ), {"n": _INDEX}).first()
            if ours:
                conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS public.{_INDEX}"))
        finally:
            conn.execute(text("RESET lock_timeout"))
