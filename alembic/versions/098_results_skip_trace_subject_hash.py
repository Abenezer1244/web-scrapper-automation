"""results.skip_trace_subject_hash: which OWNER a settled answer was bought for (098).

The duplicate-reuse passes in `_reuse_enrichment_for_duplicates` copy a settled
phone and email between rows joined on `dedup_hash = sha256(parcel|address)`, a
frozen billing key that carries NO owner name. So run 1 traces owner Alice, run 3
re-scrapes the same parcel now owned by heir Bob, the hashes match, and Bob's lead
inherits Alice's contacts.

The obvious fix -- recompute each row's subject and compare -- does not work, and
this column exists because of why. `party_name` is MUTATED after the fact by owner
recovery and enrichment. Once it is, recomputing the source row's subject yields
the CURRENT owner, which equals the target's subject, while the phone stored on
that row still belongs to the PREVIOUS one. The comparison passes and copies
exactly the leak it was added to prevent. Trace type is not on `results` at all,
None and '' are not recoverable after the fact, and pre-cutover answers were
produced under the legacy address-only key.

So the subject is RECORDED when it is known rather than reconstructed later:
written from the payload actually built (enqueue), from the pending row that was
actually submitted (ingest), and by the dispatcher's known-answer sweep.

  <64 hex>  the v2 lookup_subject_key this row's answer was bought for.
  NULL      settled before this column existed, or never settled. Fails CLOSED:
            a NULL never donates PII to another row and never receives it.

No backfill and no default, deliberately. There is no sound way to reconstruct a
historical subject, and guessing one is the leak. Historical rows simply stop
donating; the v2 cache read serves the ones whose subject is genuinely the same.

Lock safety: same shape as 097. A nullable ADD COLUMN with no default is
catalog-only (no table rewrite) but still takes a brief ACCESS EXCLUSIVE lock, so
lock_timeout makes the boot migration fail fast instead of queueing every request
behind a long transaction on results. The index is created CONCURRENTLY in an
autocommit block so it never blocks reads or writes on a table this size.

Revision ID: 098
Revises: 097
Create Date: 2026-09-19
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "098"
down_revision = "097"
branch_labels = None
depends_on = None

_INDEX = "ix_results_skip_trace_subject_hash"


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.add_column(
        "results", sa.Column("skip_trace_subject_hash", sa.String(64), nullable=True)
    )
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        # SET LOCAL above ended with its transaction; this block runs outside one,
        # so the timeout is set (and reset) here (the 097 pattern, Codex).
        conn.execute(text("SET lock_timeout = '5s'"))
        try:
            # Partial: only settled rows carry a hash, and the reuse passes only
            # ever look those up. Keeps the index small on a table that is mostly
            # never-traced rows.
            conn.execute(text(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
                "ON results (user_id, skip_trace_subject_hash) "
                "WHERE skip_trace_subject_hash IS NOT NULL"
            ))
        finally:
            conn.execute(text("RESET lock_timeout"))


def downgrade() -> None:
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        conn.execute(text("SET lock_timeout = '5s'"))
        try:
            conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}"))
        finally:
            conn.execute(text("RESET lock_timeout"))
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.drop_column("results", "skip_trace_subject_hash")
