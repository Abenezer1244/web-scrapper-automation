"""Grant bridgeleads_system access to external_source_health (fixes migration 083).

Migration 083 created the table but granted nothing. Every other SYSTEM-written
table (audit_events, batch_runs, notifications, county_connectors) carries
SELECT/INSERT/UPDATE for `bridgeleads_system` plus a `<table>_system` RLS policy;
083 shipped neither, so in production the worker got:

    permission denied for table external_source_health

The source-health gate is written to fall through to "allowed" when it cannot
READ health — health infrastructure must never block real work — so this failed
SAFE but SILENT: the gate was inert in production and nothing said so.

Grants mirror the audit_events shape exactly:
  * SELECT/INSERT/UPDATE, deliberately NOT DELETE — no app path deletes health
    rows; recovery flips status to 'healthy', it never removes the row (and the
    history is the point).
  * RLS is enabled on this table, so a GRANT alone is not enough: without a
    policy the role is permitted but every row is filtered out. `_system` gets
    FOR ALL USING (true) WITH CHECK (true) because there is exactly one row per
    source globally — the block is on our egress IP, not on a tenant.
  * `bridgeleads_app` gets NOTHING. This is read by workers only; the API never
    touches it.

Wrapped in DO-blocks keyed on pg_roles so local/CI databases without the RLS
roles provisioned (they run as a single superuser) are a clean no-op — the
convention established by migration 029.

Revision ID: 084
Revises: 083
Create Date: 2026-07-30
"""

from alembic import op

revision = "084"
down_revision = "083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
                GRANT SELECT, INSERT, UPDATE ON public.external_source_health
                    TO bridgeleads_system;
                DROP POLICY IF EXISTS external_source_health_system
                    ON public.external_source_health;
                CREATE POLICY external_source_health_system
                    ON public.external_source_health
                    FOR ALL TO bridgeleads_system
                    USING (true) WITH CHECK (true);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
                DROP POLICY IF EXISTS external_source_health_system
                    ON public.external_source_health;
                REVOKE SELECT, INSERT, UPDATE ON public.external_source_health
                    FROM bridgeleads_system;
            END IF;
        END $$;
        """
    )
