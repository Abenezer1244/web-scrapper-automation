"""The runtime system role may READ alembic_version (116).

Worker and beat no longer migrate: scripts/wait_for_schema.py blocks their boot
until the schema their code needs is applied, reading alembic_version on the
runtime role (bridgeleads_system). 027 enabled RLS on alembic_version with no
policy for that role, so it saw zero rows (verified in production 2026-10-10:
bridgeleads_system, rolbypassrls false, 0 rows; the owner role saw '115') and the
wait could never succeed. A revision id is not sensitive, so this is SELECT plus a
role-targeted read policy, nothing else. Role-guarded like 114 (CI has no runtime
roles); mirrored in scripts/provision_rls_roles.sql.

Revision ID: 116
Revises: 115
Create Date: 2026-10-10
"""
from alembic import op
from sqlalchemy import text

revision = "116"
down_revision = "115"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute(
        """
        DO $schema_read$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
                GRANT SELECT ON public.alembic_version TO bridgeleads_system;
                DROP POLICY IF EXISTS alembic_version_system_select ON public.alembic_version;
                CREATE POLICY alembic_version_system_select ON public.alembic_version
                    FOR SELECT TO bridgeleads_system USING (true);
            END IF;
        END
        $schema_read$;
        """
    )


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    # SELECT is not revoked: provision_rls_roles.sql grants it on every table, so
    # it predates 116 and revoking it here would take away more than 116 added.
    op.execute("DROP POLICY IF EXISTS alembic_version_system_select ON public.alembic_version")
