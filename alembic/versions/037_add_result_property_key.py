"""Add results.property_key (037) — Phase 3 combine/overlap join key.

Additive, nullable, NO in-migration backfill: results can be large and
scripts/migrate.py runs migrations under a ~900s advisory lock on API boot, so a
backfill here would risk the deploy. Historical seeding lives in
scripts/backfill_result_property_key.py (offline, best-effort, idempotent).
Forward accrual happens in workers/tasks.py post-enrichment.

property_key is the SAME post-enrichment sha256(parcel|address) that
property_list_membership stores (property_identity.compute_property_key). It
lets Phase 3's intersection export join overlap property_keys back to full
Result rows for the leads themselves. dedup_hash is PRE-enrichment and can
differ, so it cannot be reused for this join.

Index is PARTIAL (WHERE property_key IS NOT NULL): the 3B join only ever filters
non-null keys (weak rows are NULL), so indexing nulls wastes space. Plain (not
CONCURRENT) create_index — consistent with migrations 033/034 and safe at the
current results size; CONCURRENTLY cannot run inside the advisory-lock txn.

Revision ID: 037
Revises: 036
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa

revision = "037"
down_revision = "036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("results", sa.Column("property_key", sa.String(length=64), nullable=True))
    op.create_index(
        "ix_results_user_property_key",
        "results",
        ["user_id", "property_key"],
        postgresql_where=sa.text("property_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_results_user_property_key", table_name="results")
    op.drop_column("results", "property_key")
