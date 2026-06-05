"""Add results.delinquent_amount + delinquent_bill_year (038) — Phase 4 tax filters.

Additive, nullable, NO in-migration backfill (results can be large; migrations
run under a ~900s advisory lock on API boot). Historical seeding lives in
scripts/backfill_result_tax_fields.py (offline, source-gated, idempotent).
Forward population happens in workers/tasks.py at insert.

Structured tax-delinquency columns let users filter tax_delinquent leads by
amount owed and time delinquent. Currently ONLY King exposes structured data
(Socrata: delinquent_amount + bill_year, stamped into enrichment_data); other
counties' tax_delinquent rows are recorder keyword matches and stay NULL, so
they never match an amount/age filter.

`delinquent_bill_year` (not a stored "months" value): delinquency age is derived
at query time from the bill year (King bills 01/01/bill_year), so it never goes
stale. Partial indexes — the filter only ever touches non-null structured rows.
Plain (not CONCURRENT) create_index, consistent with 033/034/037.

Revision ID: 038
Revises: 037
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa

revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("results", sa.Column("delinquent_amount", sa.Numeric(12, 2), nullable=True))
    op.add_column("results", sa.Column("delinquent_bill_year", sa.Integer(), nullable=True))
    op.create_index(
        "ix_results_job_delinquent_amount",
        "results",
        ["job_id", "delinquent_amount"],
        postgresql_where=sa.text("delinquent_amount IS NOT NULL"),
    )
    op.create_index(
        "ix_results_job_delinquent_year",
        "results",
        ["job_id", "delinquent_bill_year"],
        postgresql_where=sa.text("delinquent_bill_year IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_results_job_delinquent_year", table_name="results")
    op.drop_index("ix_results_job_delinquent_amount", table_name="results")
    op.drop_column("results", "delinquent_bill_year")
    op.drop_column("results", "delinquent_amount")
