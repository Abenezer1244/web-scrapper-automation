"""NTS Tier 1: auction fields on results (matched from nts_notices). (059)

The matcher (workers/nts_matcher beat + inline) attaches a high-confidence NTS
notice to a pre_foreclosure Result and writes the investor-headline fields here:
auction_date (the urgency clock), default_amount (principal owing), the match
confidence, and a ref to the source notice. Descriptive fields (trustee, ts#,
beneficiary, location) ride in enrichment_data["nts"] (export passthrough), like
the Phase-1 pattern — only the filter/sort-worthy ones get columns.

All NULLABLE = instant ALTER on the live RLS-FORCE'd results table (no rewrite).
results is already a tenant table with its RLS policies; no policy change here.
nts_notice_id is a plain UUID ref (NOT an enforced FK): Result is tenant-scoped
and nts_notices is the system-only shared cache, so a cross-RLS-domain FK would
add lock/permission surface for no integrity win — the matcher sets it from a row
it just read.

Revision ID: 059
Revises: 058
Create Date: 2026-06-12
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "059"
down_revision = "058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("results", sa.Column("auction_date", sa.Date(), nullable=True))
    op.add_column("results", sa.Column("default_amount", sa.Numeric(12, 2), nullable=True))
    op.add_column("results", sa.Column("nts_match_confidence", sa.Numeric(3, 2), nullable=True))
    op.add_column("results", sa.Column("nts_notice_id", UUID(as_uuid=False), nullable=True))
    # The filter/sort index ix_results_job_auction_date is created OUT-OF-BAND via
    # scripts/create_owner_flag_indexes.sql (CREATE INDEX CONCURRENTLY) — a plain
    # CREATE INDEX here would scan + lock the live 310k results table inside the
    # alembic txn, behind the held ALTER locks (Codex, same as the owner-flag indexes).


def downgrade() -> None:
    op.drop_column("results", "nts_notice_id")
    op.drop_column("results", "nts_match_confidence")
    op.drop_column("results", "default_amount")
    op.drop_column("results", "auction_date")
