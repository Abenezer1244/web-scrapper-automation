"""The API role may no longer read county_records (119).

county_records lost its only API reader when UX 3.13 retired GET
/scrapers/{id}/records, and its only writer when #522 retired the daily cache
scrape. The weekly purge (worker, bridgeleads_system) is the last user, so the
app's SELECT and its read policy go: least privilege for a table of scraped
homeowner data that nothing in the API reads.

Both read policies are dropped unconditionally: county_records_app_read (the
cutover script's, in production) and county_records_shared_read (028's, which
targets PUBLIC and is what a migrations-only database has). The REVOKE is
role-guarded like 116 because CI has no runtime roles. bridgeleads_system keeps
its grants and county_records_system policy; Supabase's anon / authenticated
grants are untouched and still read 0 rows (RLS is forced and no policy targets
them). Mirrored in scripts/provision_rls_roles.sql and the cutover scripts.

Revision ID: 119
Revises: 118
Create Date: 2026-10-20
"""
from alembic import op
from sqlalchemy import text

revision = "119"
down_revision = "118"
branch_labels = None
depends_on = None

_READ = "NULLIF(current_setting('app.current_user_id', true), '') IS NOT NULL"


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute("DROP POLICY IF EXISTS county_records_app_read ON public.county_records")
    op.execute("DROP POLICY IF EXISTS county_records_shared_read ON public.county_records")
    op.execute(
        """
        DO $no_app_read$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_app') THEN
                REVOKE ALL ON public.county_records FROM bridgeleads_app;
            END IF;
        END
        $no_app_read$;
        """
    )


def downgrade() -> None:
    # Back to each database's 118 shape: the role-provisioned one had the app
    # grant + county_records_app_read; a migrations-only one had 028's policy.
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute(
        f"""
        DO $app_read$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_app') THEN
                GRANT SELECT ON public.county_records TO bridgeleads_app;
                CREATE POLICY county_records_app_read ON public.county_records
                    FOR SELECT TO bridgeleads_app USING ({_READ});
            ELSE
                CREATE POLICY county_records_shared_read ON public.county_records
                    FOR SELECT USING ({_READ});
            END IF;
        END
        $app_read$;
        """
    )
