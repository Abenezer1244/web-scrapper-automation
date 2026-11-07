"""Close the Supabase Data API: anon/authenticated reach nothing in public (134).

FINDING 2026-11-07. Supabase's default privileges for `postgres` in schema public grant
anon and authenticated ALL on every new table and sequence and EXECUTE on every new
function, and the Data API serves schema public to the publishable (anon) key. Rows were
safe (every table has RLS and no policy for these roles), but SECURITY DEFINER
privacy_rule_rows() (migration 129, meant for bridgeleads_system only) was executable by
anon, authenticated and service_role: callable as POST /rest/v1/rpc/privacy_rule_rows.

Nothing of ours uses these roles: the API and workers connect as bridgeleads_app /
_system / _purge (direct grants, not members of anon/authenticated) and the browser never
talks to Supabase. So:
  - revoke everything anon/authenticated hold on public tables, sequences and functions;
  - revoke the same from the migration owner's default privileges in public, so new
    objects stop inheriting them;
  - revoke service_role's EXECUTE on the functions this role owns, which included the
    definer functions privacy_rule_rows, activation_funnel(_v2) and grant_referral_credit
    (BYPASSRLS does not cover EXECUTE; nothing of ours calls through service_role).
Only objects the migration role owns are touched (on Supabase `postgres` is not a superuser
and some functions belong to bridgeleads_purge); extension functions are left alone.
Each role is handled only if it exists (CI and local clusters have none). The postcondition
fails the migration if any of them can still reach a public table or call a SECURITY
DEFINER function; the hourly beat audit (audit_rls_system_policies) checks the same.

Downgrade is a deliberate no-op: restoring public API access to the data would reopen
the finding.

Revision ID: 134
Revises: 133
Create Date: 2026-11-07
"""
from alembic import op
from sqlalchemy import text

revision = "134"
down_revision = "133"
branch_labels = None
depends_on = None

# Public so the tests can apply exactly this body inside their own transaction.
UPGRADE_SQL = """
DO $api_roles$
DECLARE r text; o record;
BEGIN
  -- Object by object, only what this role owns: on Supabase `postgres` is not a superuser
  -- and some objects belong to bridgeleads_purge, where REVOKE ... ON ALL ... would fail.
  -- Anything left reachable is caught by the postcondition below, loudly.
  FOR r IN SELECT rolname FROM pg_roles WHERE rolname IN ('anon', 'authenticated') LOOP
    FOR o IN SELECT c.oid::regclass AS obj, c.relkind FROM pg_class c
             JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
             WHERE c.relkind IN ('r', 'v', 'm', 'p', 'f', 'S')
               AND c.relowner = current_user::regrole LOOP
      EXECUTE format('REVOKE ALL ON %s %s FROM %I',
                     CASE WHEN o.relkind = 'S' THEN 'SEQUENCE' ELSE 'TABLE' END, o.obj, r);
    END LOOP;
    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM %I', r);
    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM %I', r);
  END LOOP;
  -- Functions, service_role too (it held EXECUTE on the definer functions; nothing of
  -- ours calls through it). Extension functions (pg_trgm) are left to their extension.
  FOR r IN SELECT rolname FROM pg_roles
           WHERE rolname IN ('anon', 'authenticated', 'service_role') LOOP
    FOR o IN SELECT p.oid::regprocedure AS fn FROM pg_proc p
             JOIN pg_namespace n ON n.oid = p.pronamespace AND n.nspname = 'public'
             WHERE p.proowner = current_user::regrole AND p.prokind IN ('f', 'p')
               AND NOT EXISTS (SELECT 1 FROM pg_depend d
                               WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid
                                 AND d.deptype = 'e') LOOP
      EXECUTE format('REVOKE ALL ON ROUTINE %s FROM %I', o.fn, r);
    END LOOP;
    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM %I', r);
  END LOOP;

  IF EXISTS (
    SELECT 1 FROM pg_roles ro, pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
    WHERE ro.rolname IN ('anon', 'authenticated') AND c.relkind IN ('r', 'v', 'm', 'p')
      AND (has_table_privilege(ro.oid, c.oid, 'SELECT')
           OR has_table_privilege(ro.oid, c.oid, 'INSERT')
           OR has_table_privilege(ro.oid, c.oid, 'UPDATE')
           OR has_table_privilege(ro.oid, c.oid, 'DELETE')
           OR has_table_privilege(ro.oid, c.oid, 'TRUNCATE'))) THEN
    RAISE EXCEPTION 'migration 134: anon/authenticated can still reach a public table';
  END IF;
  IF EXISTS (
    SELECT 1 FROM pg_roles ro, pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace AND n.nspname = 'public'
    WHERE ro.rolname IN ('anon', 'authenticated', 'service_role') AND p.prosecdef
      AND p.prorettype <> 'event_trigger'::regtype
      AND has_function_privilege(ro.oid, p.oid, 'EXECUTE')) THEN
    RAISE EXCEPTION 'migration 134: a Supabase API role can still call a SECURITY DEFINER function';
  END IF;
END $api_roles$;
"""


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute(text(UPGRADE_SQL))


def downgrade() -> None:
    # Deliberate no-op: see the module docstring.
    pass
