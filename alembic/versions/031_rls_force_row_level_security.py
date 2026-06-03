"""Phase 4 RLS cutover: FORCE ROW LEVEL SECURITY — NO-OP PLACEHOLDER.

FORCE moved OUT of Alembic into an idempotent operator script:
`scripts/apply_rls_force.sql` (Codex cross-phase review), for the same reason as
migration 030: it depends on the out-of-band cutover roles + the role-targeted
policies, and an auto-running role-conditional migration would no-op-and-consume
itself before the cutover. apply_rls_force.sql is the last staged step and keeps
the SECURITY DEFINER owner / BYPASSRLS guard.

Revision ID: 031
Revises: 030
Create Date: 2026-06-02
"""

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Intentional no-op — see module docstring. Real DDL lives in
    # scripts/apply_rls_force.sql.
    pass


def downgrade() -> None:
    pass
