"""Account deletion: the beat worker may READ account_deletions (114).

The P3b beat task (bridgeleads_system) must list the rows still owing a Stripe
cancel/uncancel, the "scheduled for deletion" email or an ops alert. 112 gave the
worker no access at all; every write still goes only through the 113 definer
functions (the guard trigger refuses any other writer), so this is SELECT alone,
plus the matching role-targeted RLS policy. Role-guarded like 112/113 (CI has no
runtime roles); mirrored in scripts/provision_rls_roles.sql.

Revision ID: 114
Revises: 113
Create Date: 2026-10-06
"""
from alembic import op
from sqlalchemy import text

revision = "114"
down_revision = "113"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute(
        """
        DO $worker_read$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
                GRANT SELECT ON public.account_deletions TO bridgeleads_system;
                DROP POLICY IF EXISTS account_deletions_system_select ON public.account_deletions;
                CREATE POLICY account_deletions_system_select ON public.account_deletions
                    FOR SELECT TO bridgeleads_system USING (true);
            END IF;
        END
        $worker_read$;
        """
    )


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute(
        """
        DO $worker_read$
        BEGIN
            DROP POLICY IF EXISTS account_deletions_system_select ON public.account_deletions;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
                REVOKE SELECT ON public.account_deletions FROM bridgeleads_system;
            END IF;
        END
        $worker_read$;
        """
    )
