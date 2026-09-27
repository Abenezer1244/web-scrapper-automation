"""The dispatcher's per-account keyset frontier gets its index (103).

Phase 1b-1b-ii-c. The credit cap's refill (ii-b) re-ranked every eligible queued
row on every round with a window sort, which failed its performance gate at
117k queued rows (~440 ms a round). ii-c-2 replaces that with a per-account
keyset frontier: discover the accounts with queued rows of one trace type (a
loose index scan, one probe per account), then walk each account's rows in
(enqueued_at, id) order from where the previous round stopped. Both need this
partial index, keyed trace_type FIRST so discovery for one type skips across
user_id without walking the other type's rows (Codex ii-c consult F2):

    (trace_type, user_id, enqueued_at, id) WHERE status = 'queued'

Built CONCURRENTLY with 102's discipline, except that identity is structural
(see below) plus indisvalid; an invalid or wrong-shaped index of this name on THIS table is dropped and rebuilt (safe only
because migrations are serialized by scripts/migrate.py's advisory lock, which
start.sh boots through); a same-named index on ANY OTHER table aborts and is
never dropped. Listed in alembic/env.py CONCURRENT_INDEXES so autogenerate never
proposes a blocking plain build.

Identity is STRUCTURAL, read from the catalogs (Codex ii-c-1 re-review): a
whole-string match against one server's rendering would, on a server that renders
it differently, never match again and rebuild on every run of this migration.
Only the predicate needs a deparse, and it is compared with casts, parentheses
and whitespace stripped. (For the record: production PostgreSQL 17.6, read
2026-09-27, renders 102's objects byte for byte as the local PG16 does.)

No data change, no constraint, no trigger. The existing
ix_pending_skip_trace_dispatch stays: other queued-row scans still use it (F7).
Production held ~941 pending rows on 2026-09-26, so the build is sub-second;
lock_timeout bounds only the waits for locks, as in 102.

Backward safe: nothing requires the index to exist; ii-c-2 only runs faster
with it. ii-c-2 deploys only after this index is verified in production (F6).

Revision ID: 103
Revises: 102
Create Date: 2026-09-27
"""
import re

from alembic import op
from sqlalchemy import text

revision = "103"
down_revision = "102"
branch_labels = None
depends_on = None

_INDEX = "ix_pending_skip_trace_queued_frontier"
_KEY_COLUMNS = ["trace_type", "user_id", "enqueued_at", "id"]
# How PostgreSQL 16 and 17 render it (for operators and tests). NOT the identity
# check: that is structural, below (Codex ii-c-1 re-review).
_INDEX_DEF = (
    f"CREATE INDEX {_INDEX} ON public.pending_skip_trace_rows USING btree "
    f"(trace_type, user_id, enqueued_at, id) WHERE ((status)::text = 'queued'::text)"
)

# Identity from the catalogs, not from one server's rendering of the DDL: a valid,
# non-unique btree on exactly these key columns in this order, with no INCLUDE
# columns, no expressions, ascending, the default operator class and each
# column's own collation. The predicate is the one part only a deparse can show;
# it is compared with casts, parentheses and whitespace stripped, so a
# difference in how a server renders `status = 'queued'` cannot read as a
# different index.
_SHAPE_SQL = """
SELECT i.indisvalid, i.indisunique, i.indisexclusion, i.indislive, i.indisready,
       i.indnatts, i.indnkeyatts,
       i.indexprs IS NULL AS no_exprs, am.amname,
       ARRAY(SELECT a.attname::text
             FROM unnest(i.indkey::int2[]) WITH ORDINALITY k(attnum, ord)
             JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
             ORDER BY k.ord) AS cols,
       (SELECT bool_and(o = 0) FROM unnest(i.indoption::int2[]) o) AS all_asc,
       (SELECT bool_and(oc.opcdefault)
          FROM unnest(i.indclass::oid[]) cls(oid)
          JOIN pg_opclass oc ON oc.oid = cls.oid) AS default_opclasses,
       (SELECT bool_and(k.coll = a.attcollation)
          FROM unnest(i.indcollation::oid[], i.indkey::int2[]) k(coll, attnum)
          JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
       ) AS column_collations,
       pg_get_expr(i.indpred, i.indrelid) AS predicate,
       EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = i.indexrelid)
           AS backs_constraint
FROM pg_class c
JOIN pg_namespace cn ON cn.oid = c.relnamespace
JOIN pg_index i ON i.indexrelid = c.oid
JOIN pg_am am ON am.oid = c.relam
JOIN pg_class t ON t.oid = i.indrelid
JOIN pg_namespace tn ON tn.oid = t.relnamespace
WHERE c.relname = :n AND cn.nspname = 'public'
  AND t.relname = 'pending_skip_trace_rows' AND tn.nspname = 'public'
"""


_QUOTED = re.compile(r"'(?:[^']|'')*'")
# Only these casts, and only OUTSIDE quoted literals: `'queued::text'` is a
# different literal and must stay different (Codex ii-c-1 round 3).
_CAST = re.compile(r"::(?:text|character varying|varchar)\b")


def _normalized_predicate(predicate: str | None) -> str:
    """`((status)::text = 'queued'::text)` -> `status='queued'`. Casts, parentheses and
    whitespace are stripped between literals; each quoted literal is kept verbatim."""
    s = predicate or ""
    out, pos = [], 0
    for m in _QUOTED.finditer(s):
        out.append(_strip_outside(s[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_strip_outside(s[pos:]))
    return "".join(out)


def _strip_outside(fragment: str) -> str:
    fragment = _CAST.sub("", fragment)
    return "".join(ch for ch in fragment if ch not in "() \t\n")


def _is_right_shape(row) -> bool:
    return bool(
        row.indisvalid
        and row.indislive
        and row.indisready
        and not row.indisunique
        and not row.indisexclusion
        and row.amname == "btree"
        and row.no_exprs
        and row.indnatts == row.indnkeyatts == len(_KEY_COLUMNS)
        and list(row.cols) == _KEY_COLUMNS
        and row.all_asc
        and row.default_opclasses
        and row.column_collations
        and _normalized_predicate(row.predicate) == "status='queued'"
    )


def _build_frontier_index(conn) -> None:
    """CREATE INDEX CONCURRENTLY, checked by structural identity."""
    existing = conn.execute(text(_SHAPE_SQL), {"n": _INDEX}).first()
    right_shape = existing is not None and _is_right_shape(existing)
    if existing is not None and not right_shape and existing.backs_constraint:
        # An index that enforces a constraint (an EXCLUDE, say) cannot be dropped
        # as an index, and it is somebody's constraint: say so rather than fail
        # on a raw error, and leave it.
        raise RuntimeError(
            f"Migration 103 ABORTED: {_INDEX} on public.pending_skip_trace_rows backs a "
            f"constraint and is not the frontier index. It is NOT dropped. Drop or rename "
            f"that constraint deliberately, then re-run."
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
                f"Migration 103 ABORTED: an index named {_INDEX} already exists on "
                f"{collision}, which is not public.pending_skip_trace_rows. It is NOT "
                f"dropped, because it may be something else's. Rename or remove it "
                f"deliberately, then re-run."
            )
    if not right_shape:
        conn.execute(text(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
            f"ON public.pending_skip_trace_rows (trace_type, user_id, enqueued_at, id) "
            f"WHERE status = 'queued'"
        ))


def upgrade() -> None:
    # CONCURRENTLY cannot run in a transaction: autocommit_block commits the
    # migration transaction on entry (the 101 lesson), and this is the only step.
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        conn.execute(text("SET lock_timeout = '5s'"))
        try:
            _build_frontier_index(conn)
        finally:
            conn.execute(text("RESET lock_timeout"))


def downgrade() -> None:
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
