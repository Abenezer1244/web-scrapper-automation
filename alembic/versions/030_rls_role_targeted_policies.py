"""Phase 2c RLS cutover: role-targeted policies — NO-OP PLACEHOLDER.

The role-targeted policy installation moved OUT of Alembic into an idempotent
operator script: `scripts/apply_rls_cutover_policies.sql` (Codex cross-phase
review).

Why it cannot live here: `alembic upgrade head` runs on every deploy, but the
cutover roles (bridgeleads_app / bridgeleads_system) are created out-of-band by
scripts/provision_rls_roles.sql during the staged cutover. A role-conditional
migration would run as a NO-OP on the first post-merge deploy (roles absent),
advance alembic_version, and then never re-run when the roles are later
provisioned — silently skipping the entire cutover. So this revision is an
intentional no-op placeholder that preserves the migration chain; the real,
re-runnable DDL is in scripts/apply_rls_cutover_policies.sql.

Run order (see docs/security/RLS-CUTOVER-RUNBOOK.md):
  alembic upgrade head            # 029 objects; 030/031 no-op
  provision_rls_roles.sql         # create roles + grants
  apply_rls_cutover_policies.sql  # role-targeted policies (this revision's work)
  apply_rls_force.sql             # FORCE (031's work), last

Revision ID: 030
Revises: 029
Create Date: 2026-06-02
"""

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Intentional no-op — see module docstring. Real DDL lives in
    # scripts/apply_rls_cutover_policies.sql.
    pass


def downgrade() -> None:
    pass
