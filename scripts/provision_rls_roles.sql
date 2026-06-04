-- ============================================================================
-- provision_rls_roles.sql — least-privilege DB roles for the RLS cutover
-- ----------------------------------------------------------------------------
-- Origin: SQL-injection audit (2026-06-02, Claude × Codex). No SQLi exists in
-- the app; the real blast-radius risk is the single over-privileged BYPASSRLS
-- role. This script creates two scoped login roles + verifies them. See:
--   tasks/rls-cutover-todo.md            (the staged plan)
--   docs/security/RLS-CUTOVER-RUNBOOK.md (how/when to run each phase)
--
-- THIS SCRIPT IS PHASE 0. It only CREATES roles + GRANTs table privileges and
-- verifies them. It does NOT change which role the app connects as, does NOT
-- enable RLS enforcement, and does NOT add the system-role policy carve-out
-- (Phase 2). Safe and inert until connection strings are repointed (Phase 3).
--
-- Run manually as a superuser / the schema owner, passing passwords as psql
-- variables so secrets never land in version control:
--
--   psql "$ADMIN_DATABASE_URL" \
--     -v app_pw="$(openssl rand -base64 32)" \
--     -v sys_pw="$(openssl rand -base64 32)" \
--     -f scripts/provision_rls_roles.sql
--
-- Codex review (Phase 0 gate) drove the structure below:
--   * app role write-grants are split from read-only grants (least privilege);
--   * safety flags are re-asserted unconditionally on every run;
--   * passwords are set ONLY at role creation — a rerun does NOT rotate them
--     (so it can't silently break live connections after Phase 3);
--   * the whole thing runs in one transaction with up-front var validation;
--   * a DDL fail-fast aborts if either role can CREATE in schema public.
-- ============================================================================

\set ON_ERROR_STOP on

-- ── Validate required psql vars before touching anything ────────────────────
-- RAISE EXCEPTION (not \quit) so psql exits non-zero under ON_ERROR_STOP —
-- \quit exits 0 by default and would let automation treat a failed guard as
-- success (Codex re-review). RAISE aborts with a non-zero status portably.
\if :{?app_pw}
\else
  \echo '>>> ERROR: app_pw is not set. Pass  -v app_pw="$(openssl rand -base64 32)"'
  DO $$ BEGIN RAISE EXCEPTION 'app_pw not set'; END $$;
\endif
\if :{?sys_pw}
\else
  \echo '>>> ERROR: sys_pw is not set. Pass  -v sys_pw="$(openssl rand -base64 32)"'
  DO $$ BEGIN RAISE EXCEPTION 'sys_pw not set'; END $$;
\endif

BEGIN;

-- ── Role 1: bridgeleads_app — FastAPI request traffic ───────────────────────
-- Grants are scoped to exactly what the request path touches (verified against
-- src/api/routes/* and src/db/models.py). NO DELETE anywhere; NO write on
-- worker-only tables (results, job_logs, delivered_records, skip_trace_*).
SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_app') AS create_app \gset
\if :create_app
  CREATE ROLE bridgeleads_app LOGIN PASSWORD :'app_pw'
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
\else
  \echo '>>> Role bridgeleads_app already exists — password NOT rotated (by design).'
\endif
-- Re-assert LOGIN, then VERIFY the protected attributes (do NOT ALTER them):
-- Supabase's `postgres` admin is NOT a superuser, so it cannot ALTER the
-- SUPERUSER/BYPASSRLS attributes even to NO ("only roles with SUPERUSER may
-- alter roles with the SUPERUSER attribute"). CREATE already set them to the
-- safe defaults, and a non-superuser can never grant SUPERUSER/BYPASSRLS, so
-- they cannot drift upward — verifying is sufficient and portable. (Discovered
-- running this live against Supabase.)
ALTER ROLE bridgeleads_app LOGIN;
DO $verify_app$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_app'
               AND (rolsuper OR rolbypassrls)) THEN
        RAISE EXCEPTION 'bridgeleads_app must be NOSUPERUSER + NOBYPASSRLS';
    END IF;
END
$verify_app$;

GRANT USAGE ON SCHEMA public TO bridgeleads_app;

-- Tables the request path INSERTs/UPDATEs (create scraper/job, register/login,
-- password change, record-view upsert, referral grant). No DELETE — user
-- "deletes" are soft (jobs.status='cancelled', scraper_configs.active=false).
GRANT SELECT, INSERT, UPDATE ON
    users,              -- register (INSERT), password/profile/plan (UPDATE)
    scraper_configs,    -- create (INSERT), edit + soft-delete (UPDATE)
    jobs,               -- create (INSERT), cancel (UPDATE)
    user_record_views   -- INSERT ... ON CONFLICT DO UPDATE (scrapers.py:469)
TO bridgeleads_app;

-- SELECT + INSERT (append/read, never UPDATE/DELETE):
--   county_connectors — read registry + INSERT for POST /connectors (scrapers.py:313)
--   password_history  — read for reuse-check + append new rows (auth.py); rows are
--                       immutable audit history, so no UPDATE (Codex cross-phase review).
GRANT SELECT, INSERT ON county_connectors, password_history TO bridgeleads_app;

-- Read-only: the request path SELECTs these but never writes them.
-- results + job_logs are written only by Celery workers; county_records is the
-- shared dedup cache (a migration-023 trigger blocks non-system writes anyway).
-- referral_events: /referral READS it; the WRITE is done by the SECURITY
-- DEFINER public.grant_referral_credit() (migration 029, runs as owner).
GRANT SELECT ON results, job_logs, county_records, referral_events,
    property_list_membership TO bridgeleads_app;

-- Converge to least privilege regardless of any prior (over-)grant: GRANT does
-- not remove privileges an earlier version of this script handed out, so
-- explicitly REVOKE everything the app must NOT hold (Codex review). DELETE is
-- never granted to the app on any table.
REVOKE DELETE ON users, scraper_configs, jobs, user_record_views FROM bridgeleads_app;
REVOKE UPDATE, DELETE ON county_connectors, password_history FROM bridgeleads_app;
REVOKE INSERT, UPDATE, DELETE ON
    results, job_logs, county_records, referral_events,
    property_list_membership FROM bridgeleads_app;
REVOKE ALL ON
    delivered_records, pending_skip_trace_rows, skip_trace_queues,
    skip_trace_cache, skip_trace_meter_events FROM bridgeleads_app;

-- Hard-fail if the app role still holds any DELETE, or any write on the
-- read-only / no-app tables — so a stale over-grant cannot survive a rerun.
DO $verify$
DECLARE bad int;
BEGIN
    SELECT COUNT(*) INTO bad FROM information_schema.role_table_grants
    WHERE grantee = 'bridgeleads_app'
      AND (
        privilege_type = 'DELETE'
        OR (privilege_type IN ('INSERT', 'UPDATE')
            AND table_name IN ('results', 'job_logs', 'county_records', 'referral_events',
                               'property_list_membership'))
        OR (privilege_type = 'UPDATE'
            AND table_name IN ('county_connectors', 'password_history'))
        OR table_name IN ('delivered_records', 'pending_skip_trace_rows',
                          'skip_trace_queues', 'skip_trace_cache', 'skip_trace_meter_events')
      );
    IF bad > 0 THEN
        RAISE EXCEPTION 'provision_rls_roles: bridgeleads_app still holds % '
            'disallowed privilege(s) — least-privilege convergence failed', bad;
    END IF;
END
$verify$;

-- Deliberately NOT granted to bridgeleads_app (worker-only, no request-path
-- access): delivered_records, skip_trace_cache, skip_trace_queues,
-- pending_skip_trace_rows, skip_trace_meter_events. The Tracerfy webhook
-- dispatches to a Celery worker via .delay() (webhooks.py:134), so all
-- skip-trace billing writes land on bridgeleads_system, not here.

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bridgeleads_app;

-- ── Role 2: bridgeleads_system — Celery workers + scheduler ──────────────────
-- Cross-tenant ingest/canary/watchdog/retention. SELECT/INSERT/UPDATE on all,
-- DELETE only where the code physically deletes (county_records retention,
-- src/workers/scheduler.py:521). NOBYPASSRLS — gets a named-role policy
-- carve-out in Phase 2 instead of bypassing RLS wholesale.
SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') AS create_sys \gset
\if :create_sys
  CREATE ROLE bridgeleads_system LOGIN PASSWORD :'sys_pw'
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
\else
  \echo '>>> Role bridgeleads_system already exists — password NOT rotated (by design).'
\endif
-- Re-assert LOGIN + VERIFY (not ALTER) the protected attributes — see the
-- bridgeleads_app note above (Supabase admin is not a superuser).
ALTER ROLE bridgeleads_system LOGIN;
DO $verify_sys$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system'
               AND (rolsuper OR rolbypassrls)) THEN
        RAISE EXCEPTION 'bridgeleads_system must be NOSUPERUSER + NOBYPASSRLS';
    END IF;
END
$verify_sys$;

GRANT USAGE ON SCHEMA public TO bridgeleads_system;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO bridgeleads_system;
GRANT DELETE ON county_records TO bridgeleads_system;   -- scheduler.py:521 retention only
GRANT DELETE ON property_list_membership TO bridgeleads_system;  -- overlap rollup retention prune
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bridgeleads_system;

-- ── Role 3: owner / migration role ──────────────────────────────────────────
-- The existing schema owner keeps DDL rights and is used ONLY by Alembic via
-- DATABASE_URL_MIGRATE (Phase 3). No new role here — do NOT grant DDL to
-- bridgeleads_app or bridgeleads_system.

-- ── DDL fail-fast: neither scoped role may CREATE in schema public ──────────
-- On PG15+ (Supabase) PUBLIC loses CREATE on schema public by default, so this
-- normally passes. If it fails, the fix is:
--   REVOKE CREATE ON SCHEMA public FROM PUBLIC;
-- (run separately — it affects every role, so it's an explicit decision).
SELECT has_schema_privilege('bridgeleads_app', 'public', 'CREATE')
    OR has_schema_privilege('bridgeleads_system', 'public', 'CREATE') AS can_ddl \gset
\if :can_ddl
  \echo '>>> ERROR: a scoped role can CREATE (DDL) in schema public. Aborting.'
  \echo '>>> Remedy: REVOKE CREATE ON SCHEMA public FROM PUBLIC; then rerun.'
  -- RAISE aborts the open transaction (auto-rollback) AND exits non-zero under
  -- ON_ERROR_STOP, unlike \quit which would exit 0 and hide the failure.
  DO $$ BEGIN RAISE EXCEPTION 'scoped role can CREATE in schema public; aborting provision'; END $$;
\endif

COMMIT;

-- ── Verification (informational — run after COMMIT) ─────────────────────────
-- Both roles must be rolsuper=f, rolbypassrls=f:
SELECT rolname, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole
FROM pg_roles WHERE rolname LIKE 'bridgeleads_%';
-- bridgeleads_app must have ZERO DELETE rows:
SELECT grantee, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE grantee = 'bridgeleads_app' AND privilege_type = 'DELETE';
-- bridgeleads_app must have NO write on worker-only tables (expect zero rows):
SELECT grantee, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE grantee = 'bridgeleads_app'
  AND privilege_type IN ('INSERT', 'UPDATE')
  AND table_name IN ('results', 'job_logs', 'delivered_records',
                     'skip_trace_cache', 'skip_trace_queues',
                     'pending_skip_trace_rows', 'skip_trace_meter_events');
