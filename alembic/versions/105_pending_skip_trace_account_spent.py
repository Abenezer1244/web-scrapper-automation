"""The pause state's per-account spend walk gets its index (105).

Phase 1b-1b-iii. When a spend cap binds, the dispatcher publishes when lookups can
run again: for each paused account, the moment enough of its oldest spend leaves
the rolling 24h window. That is a walk of the account's spent rows in
submitted_at order that stops at a threshold, normally within its first two
credits. 102's index leads on submitted_at with user_id only INCLUDEd, so it
cannot seek to an account: every account's walk scanned the whole window
(measured at 100k rows / 500 paused accounts: 154-189 ms; after a lowered cap
5.8 s. With this index: 86-112 ms and 0.32 s, almost all of it the per-account
totals the live cap read already pays):

    (user_id, submitted_at) INCLUDE (trace_type) WHERE submitted_at IS NOT NULL

trace_type is INCLUDEd because it is the credit weight, so the walk is index-only.
Ties on submitted_at cannot change the published time (only the threshold row's
submitted_at reaches it), so no tie-break columns are carried.

Built CONCURRENTLY with 103's discipline: identity is STRUCTURAL, read from the
catalogs, plus indisvalid; an invalid or wrong-shaped index of this name on THIS
table is dropped and rebuilt (safe only because migrations are serialized by
scripts/migrate.py's advisory lock, which start.sh boots through); a same-named
index on ANY OTHER table, or one backing a constraint, aborts and is never
dropped. Listed in alembic/env.py CONCURRENT_INDEXES so autogenerate never
proposes a blocking plain build.

INCLUDE columns change the catalog shape 103 checks (Codex iii-c consult K1):
pg_index.indkey lists the key columns THEN the INCLUDE columns (indnatts in all,
indnkeyatts keys), while indoption, indclass and indcollation cover the KEY
columns only. So the key list and the INCLUDE list are read separately, and the
order, operator-class and collation checks apply to the keys.

No data change, no constraint, no trigger. Production held ~1k pending rows in
September 2026, so the build is sub-second; lock_timeout bounds only the waits
for locks. Backward safe: nothing requires the index; the pause-state publisher
(iii-b) deploys only after this index is verified in production by
`_is_right_shape()` itself (Codex iii-c consult L2).

Revision ID: 105
Revises: 104
Create Date: 2026-09-28
"""
import re

from alembic import op
from sqlalchemy import text

revision = "105"
down_revision = "104"
branch_labels = None
depends_on = None

_INDEX = "ix_pending_skip_trace_account_spent"
_KEY_COLUMNS = ["user_id", "submitted_at"]
_INCLUDE_COLUMNS = ["trace_type"]
_PREDICATE = "submitted_atISNOTNULL"
# How PostgreSQL 16 and 17 render it (for operators and tests). NOT the identity
# check: that is structural, below.
_INDEX_DEF = (
    f"CREATE INDEX {_INDEX} ON public.pending_skip_trace_rows USING btree "
    f"(user_id, submitted_at) INCLUDE (trace_type) WHERE (submitted_at IS NOT NULL)"
)

# Identity from the catalogs: a valid, non-unique btree whose KEY columns are
# exactly (user_id, submitted_at) in that order and whose INCLUDE columns are
# exactly (trace_type); no expressions; ascending keys with the default operator
# classes and each column's own collation; the predicate compared with casts,
# parentheses and whitespace stripped.
_SHAPE_SQL = """
SELECT i.indisvalid, i.indisunique, i.indisexclusion, i.indislive, i.indisready,
       i.indnatts, i.indnkeyatts,
       i.indexprs IS NULL AS no_exprs, am.amname,
       ARRAY(SELECT a.attname::text
             FROM unnest(i.indkey::int2[]) WITH ORDINALITY k(attnum, ord)
             JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
             WHERE k.ord <= i.indnkeyatts
             ORDER BY k.ord) AS key_cols,
       ARRAY(SELECT a.attname::text
             FROM unnest(i.indkey::int2[]) WITH ORDINALITY k(attnum, ord)
             JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
             WHERE k.ord > i.indnkeyatts
             ORDER BY k.ord) AS include_cols,
       (SELECT bool_and(o = 0) FROM unnest(i.indoption::int2[]) o) AS keys_asc,
       (SELECT bool_and(oc.opcdefault)
          FROM unnest(i.indclass::oid[]) cls(oid)
          JOIN pg_opclass oc ON oc.oid = cls.oid) AS keys_default_opclasses,
       (SELECT bool_and(c.coll = a.attcollation)
          FROM unnest(i.indcollation::oid[]) WITH ORDINALITY c(coll, ord)
          JOIN unnest(i.indkey::int2[]) WITH ORDINALITY k(attnum, ord) ON k.ord = c.ord
          JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
       ) AS keys_column_collations,
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
# Only these casts, and only OUTSIDE quoted literals (as in 103).
_CAST = re.compile(r"::(?:text|character varying|varchar)\b")


def _normalized_predicate(predicate: str | None) -> str:
    """`(submitted_at IS NOT NULL)` -> `submitted_atISNOTNULL`. Casts, parentheses and
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
    """The whole identity. Also what verifies the index in production (L2), so the
    check and the build can never drift."""
    return bool(
        row.indisvalid
        and row.indislive
        and row.indisready
        and not row.indisunique
        and not row.indisexclusion
        and row.amname == "btree"
        and row.no_exprs
        and row.indnatts == len(_KEY_COLUMNS) + len(_INCLUDE_COLUMNS)
        and row.indnkeyatts == len(_KEY_COLUMNS)
        and list(row.key_cols) == _KEY_COLUMNS
        and list(row.include_cols) == _INCLUDE_COLUMNS
        and row.keys_asc
        and row.keys_default_opclasses
        and row.keys_column_collations
        and _normalized_predicate(row.predicate) == _PREDICATE
    )


def _build_account_spent_index(conn) -> None:
    """CREATE INDEX CONCURRENTLY, checked by structural identity."""
    existing = conn.execute(text(_SHAPE_SQL), {"n": _INDEX}).first()
    right_shape = existing is not None and _is_right_shape(existing)
    if existing is not None and not right_shape and existing.backs_constraint:
        # An index that enforces a constraint cannot be dropped as an index, and
        # it is somebody's constraint: say so rather than fail on a raw error.
        raise RuntimeError(
            f"Migration 105 ABORTED: {_INDEX} on public.pending_skip_trace_rows backs a "
            f"constraint and is not the account-spent index. It is NOT dropped. Drop or "
            f"rename that constraint deliberately, then re-run."
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
                f"Migration 105 ABORTED: an index named {_INDEX} already exists on "
                f"{collision}, which is not public.pending_skip_trace_rows. It is NOT "
                f"dropped, because it may be something else's. Rename or remove it "
                f"deliberately, then re-run."
            )
    if not right_shape:
        conn.execute(text(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
            f"ON public.pending_skip_trace_rows (user_id, submitted_at) "
            f"INCLUDE (trace_type) WHERE submitted_at IS NOT NULL"
        ))


def upgrade() -> None:
    # CONCURRENTLY cannot run in a transaction: autocommit_block commits the
    # migration transaction on entry (the 101 lesson), and this is the only step.
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        conn.execute(text("SET lock_timeout = '5s'"))
        try:
            _build_account_spent_index(conn)
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
