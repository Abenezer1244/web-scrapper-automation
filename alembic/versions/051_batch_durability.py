"""Add batch_runs durability columns (Track A: crash-durability hardening). (051)

Three additive, nullable/defaulted columns on batch_runs — the single durable
object for a batch execution. No backfill, no rewrite (all DEFAULT/NULL), safe
under a rolling deploy.

- dispatch_attempts   — bounds re-dispatch / pending-child re-enqueue so a
                        poisoned job or a down broker can't storm forever.
- delivery_started_at — at-most-once guard for the combined-CSV delivery email.
                        The status-guarded final write protects DB state but NOT
                        the post-commit best-effort email; a CAS on this column
                        makes "only the winner sends" durable across a re-finalize
                        (lease steal / retry).
- claim_token         — unambiguous lease ownership. claimed_at becomes a real
                        lease (reclaimable after a TTL) so a worker hard-killed
                        mid-finalize can't strand a run 'running' forever; the
                        token lets a finalize verify it still owns the lease.

Existing status='pending' (the model default + documented lifecycle) is reused
as the durable dispatch INTENT — created by the API in the same txn as the batch,
so a lost dispatch is recoverable. No new status value needed here.

Revision ID: 051
Revises: 050
Create Date: 2026-06-11
"""
import sqlalchemy as sa
from alembic import op

revision = "051"
down_revision = "050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "batch_runs",
        sa.Column(
            "dispatch_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "batch_runs",
        sa.Column("delivery_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "batch_runs",
        sa.Column("claim_token", sa.String(36), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("batch_runs", "claim_token")
    op.drop_column("batch_runs", "delivery_started_at")
    op.drop_column("batch_runs", "dispatch_attempts")
