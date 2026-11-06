"""results: the privacy suppression flag; jobs: the breakdown bucket for it (126).

Phase 6 P3 (docs/legal/audit-2026-11/06-DESIGN-PRIVACY-REQUESTS-AND-SUPPRESSION.md,
section 6): a row withheld because a person asked not to be in the product carries a
DEDICATED flag, not a value in enrichment_data's delivery_excluded_reason, so privacy
never overwrites quota or qualification state and quota never overwrites privacy.

  results.privacy_suppressed_at   when the row was withheld (NULL = not withheld)
  results.privacy_suppression_id  the concrete versioned rule row that withheld it
                                  (privacy_suppressions.id, which names one rule_id
                                  under one key_version and normalization_version),
                                  so a review after key rotation can tell exactly
                                  which rule instance applied. FK, RESTRICT: a rule row
                                  a result points at cannot be deleted (rule rows are
                                  append-only anyway).
  jobs.breakdown_privacy_withheld the run-count bucket for those rows, so the snapshot
                                  partition still accounts for every saved row
                                  (records_found = dropped_before_save + buckets).
                                  NULL on a snapshot frozen before this migration
                                  means nothing was withheld (read as 0), the same
                                  rule as 121.

The two results columns are set together or not at all (CHECK). Nothing writes them
yet: the writers are the enforcer sweep (P5) and activation (P7), and the database
still refuses any rule state but 'pending' (125's activation guard).

EXPAND ONLY, and shipped on its own before any code that reads the columns. Railway
runs migrations at API boot while the workers start at the same time, so code that
selects these columns must not deploy until this migration has applied everywhere.
Old code ignores the columns.

Lock safety: ADD COLUMN without a default is catalog-only. The FK and the CHECK are
added NOT VALID: no scan of results under the ACCESS EXCLUSIVE lock this transaction
holds. Nothing is lost by it: every existing row has both columns NULL (they are new),
which satisfies both constraints, and NOT VALID still checks every INSERT and UPDATE.
They are deliberately not VALIDATEd here: inside the same transaction that scan would
run under the exclusive lock and block the table.
Grants: results and jobs grants are table-level (scripts/provision_rls_roles.sql), so
the new columns inherit them; RLS policies are row-level and unchanged.

Forward-only in production: rolling back is a code revert. downgrade() exists for test
databases.

Revision ID: 126
Revises: 125
Create Date: 2026-11-06
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID

revision = "126"
down_revision = "125"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.add_column("results", sa.Column("privacy_suppressed_at",
                                       sa.DateTime(timezone=True), nullable=True))
    op.add_column("results", sa.Column("privacy_suppression_id",
                                       UUID(as_uuid=False), nullable=True))
    op.add_column("jobs", sa.Column("breakdown_privacy_withheld", sa.Integer(), nullable=True))
    op.execute(text(
        "ALTER TABLE results ADD CONSTRAINT fk_results_privacy_suppression "
        "FOREIGN KEY (privacy_suppression_id) REFERENCES privacy_suppressions (id) "
        "ON DELETE RESTRICT NOT VALID"
    ))
    op.execute(text(
        "ALTER TABLE results ADD CONSTRAINT ck_results_privacy_flag_pair "
        "CHECK ((privacy_suppressed_at IS NULL) = (privacy_suppression_id IS NULL)) NOT VALID"
    ))


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.drop_constraint("ck_results_privacy_flag_pair", "results", type_="check")
    op.drop_constraint("fk_results_privacy_suppression", "results", type_="foreignkey")
    op.drop_column("jobs", "breakdown_privacy_withheld")
    op.drop_column("results", "privacy_suppression_id")
    op.drop_column("results", "privacy_suppressed_at")
