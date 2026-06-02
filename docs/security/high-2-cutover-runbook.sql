-- HIGH-2 (step 2/2): RLS role-downgrade CUTOVER — MANUAL, STAGING FIRST.
-- Run by a DB admin during a deliberate cutover window. NOT an Alembic migration
-- (FORCE must be enabled LAST, after the roles exist and the app/worker code is
-- deployed on the new credentials — see docs/security/HIGH-2-rls-migration-plan.md).
-- Prereq: migration 025 (policy system-escape + WITH CHECK) already applied.

-- ─── 1. Roles (NOBYPASSRLS NOSUPERUSER; runtime roles do NOT own the tables) ───
-- The migration/owner role keeps owning the tables (FORCE in step 3 stops the
-- OWNER from bypassing RLS; the non-owner runtime roles below are subject to RLS
-- as soon as you switch credentials in step 2). Run these ONCE — replace the
-- placeholders with strong passwords from your secret manager. If a role already
-- exists, use `ALTER ROLE <name> WITH PASSWORD '...'` instead of CREATE.
CREATE ROLE bridgeleads_app    LOGIN PASSWORD 'REPLACE_APP_PW'    NOBYPASSRLS NOSUPERUSER;
CREATE ROLE bridgeleads_system LOGIN PASSWORD 'REPLACE_SYSTEM_PW' NOBYPASSRLS NOSUPERUSER;

GRANT USAGE ON SCHEMA public TO bridgeleads_app, bridgeleads_system;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
  TO bridgeleads_app, bridgeleads_system;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public
  TO bridgeleads_app, bridgeleads_system;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bridgeleads_app, bridgeleads_system;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO bridgeleads_app, bridgeleads_system;

-- ─── 2. Deploy code on the new credentials BEFORE the next step ────────────────
-- ALL THREE runtime URLs must move to the non-bypass roles, or a path still
-- bypasses RLS after FORCE:
--   DATABASE_URL        -> bridgeleads_app    (async FastAPI sessions)   <-- DO NOT FORGET
--   DATABASE_URL_SYNC   -> bridgeleads_app    (sync rls_sync_session / get_sync_db)
--   DATABASE_URL_SYSTEM -> bridgeleads_system (system_sync_session, cross-tenant)
-- Verify workers + API run green against staging on these roles, THEN continue.

-- ─── 3. FORCE RLS (enable LAST) ────────────────────────────────────────────────
-- The non-owner runtime roles (step 1) are already subject to RLS once step 2
-- switches credentials. FORCE is what stops the table OWNER (the migration role,
-- which still runs migrations) from bypassing RLS — enable it last so a
-- migration/admin connection can't silently read across tenants either.
ALTER TABLE scraper_configs        FORCE ROW LEVEL SECURITY;
ALTER TABLE jobs                   FORCE ROW LEVEL SECURITY;
ALTER TABLE results                FORCE ROW LEVEL SECURITY;
ALTER TABLE job_logs               FORCE ROW LEVEL SECURITY;
ALTER TABLE delivered_records      FORCE ROW LEVEL SECURITY;
ALTER TABLE pending_skip_trace_rows FORCE ROW LEVEL SECURITY;
ALTER TABLE skip_trace_queues      FORCE ROW LEVEL SECURITY;
ALTER TABLE password_history       FORCE ROW LEVEL SECURITY;
ALTER TABLE referral_events        FORCE ROW LEVEL SECURITY;
ALTER TABLE user_record_views      FORCE ROW LEVEL SECURITY;

-- ─── 4. Verify ─────────────────────────────────────────────────────────────────
-- (a) runtime roles are non-bypass, non-owner:
SELECT rolname, rolbypassrls, rolsuper FROM pg_roles
 WHERE rolname IN ('bridgeleads_app','bridgeleads_system');   -- expect f,f for both
-- (b) all 10 tables are FORCE:
SELECT relname, relforcerowsecurity FROM pg_class
 WHERE relname IN ('scraper_configs','jobs','results','job_logs','delivered_records',
                   'pending_skip_trace_rows','skip_trace_queues','password_history',
                   'referral_events','user_record_views');     -- expect t for all
-- (c) run tests/test_rls_isolation.py against the new roles (see that file).

-- ─── ROLLBACK (fast) ───────────────────────────────────────────────────────────
-- 1. Point env URLs back at the old BYPASSRLS role (instant).
-- 2. ALTER TABLE ... NO FORCE ROW LEVEL SECURITY;  (per table, reverses step 3)
-- The migration-025 policy clauses are backward-compatible — leave them in place.
