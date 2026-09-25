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

-- ── H1 drift tables (2026-06-12, Codex-consulted session 019ebbc2) ──────────
-- MFA tables (migrations 043/045). mfa_backup_codes is the SINGLE allowed app
-- DELETE: /auth/mfa/enable replaces the code set (auth.py:1209), /auth/mfa/disable
-- removes it (auth.py:1293), break-glass burns it (auth.py:701). Rows are the
-- caller's own secret hashes; the tenant policy bounds the blast radius. Codex
-- verdict: scoped grant beats SECURITY DEFINER fns here — do NOT generalize
-- app DELETE beyond this table (the verify block enforces that).
GRANT SELECT, INSERT, UPDATE, DELETE ON mfa_backup_codes TO bridgeleads_app;
-- mfa_break_glass_codes: app consumes (atomic UPDATE..RETURNING) + revokes
-- siblings — never INSERTs (operator script) and never DELETEs.
GRANT SELECT, UPDATE ON mfa_break_glass_codes TO bridgeleads_app;
-- Batch scrape (migration 050/052): POST /batches INSERTs the batch + the
-- durable pending batch_runs intent; GETs read both. Lifecycle UPDATEs are
-- worker-only (completion barrier, recovery sweep).
GRANT SELECT, INSERT ON scraper_batches, batch_runs TO bridgeleads_app;
-- audit_events (migration 055): audit_log() INSERTs from a background task
-- (security.py:546) with no user context — INSERT only, no app read path.
GRANT INSERT ON audit_events TO bridgeleads_app;
-- notifications (migration 065): system (workers) WRITEs; the app only READs
-- its own feed (GET) and UPDATEs read_at (mark-read). No app INSERT/DELETE.
GRANT SELECT, UPDATE ON notifications TO bridgeleads_app;
-- dialer_deliveries (migration 041): the dialer-replay route resets this
-- user's FAILED outbox rows to pending (UPDATE needs SELECT for its WHERE).
-- INSERT/DELETE stay worker-only.
GRANT SELECT, UPDATE ON dialer_deliveries TO bridgeleads_app;
-- pending_registrations (migration 074): the email-verified signup staging
-- table. The app (register) INSERTs a row; verify SELECTs the row and DELETEs
-- all sibling rows for the address. Pre-account (no user_id) so it is broad
-- like users. This is the SECOND allowlisted app DELETE (see the verify block) —
-- the row is unverified pre-account staging, not tenant data.
GRANT SELECT, INSERT, DELETE ON pending_registrations TO bridgeleads_app;
-- stripe_webhook_events (migration 095): the Stripe webhook (API, no tenant GUC)
-- checks and records handled event ids in its own transaction. Not tenant data.
-- Append-only: no UPDATE, no DELETE.
GRANT SELECT, INSERT ON stripe_webhook_events TO bridgeleads_app;

-- contact_lookup_* (migration 101): the "look up contacts" action ledger.
-- The API creates an action and its quoted set at confirm time and reads the
-- status page; it never transitions a verdict. UPDATE is granted on the ACTION
-- only, for the API's own created -> dispatching hop. The verdict table gets
-- SELECT + INSERT and no UPDATE, and the event log is append-only for BOTH
-- roles -- history the writer can edit is not evidence in a billing dispute.
-- A grant cannot express "insert only an INITIAL verdict"; migration 101's
-- trigger does that.
-- MUST come before the column grant below. A table-level REVOKE also
-- revokes the matching COLUMN privileges (verified against Postgres, not
-- assumed), so running it later in this file would silently strip the
-- column grant and leave the API unable to dispatch at all. This is also
-- the convergence step for a database that received an earlier grant: it
-- wipes the old table-wide UPDATE AND the old four-column grant alike.
REVOKE UPDATE ON contact_lookup_actions FROM bridgeleads_app;
GRANT SELECT, INSERT ON contact_lookup_actions TO bridgeleads_app;
-- Column-level, not table-wide. A policy constrains WHICH ROWS and never
-- WHICH COLUMNS, so a table-wide UPDATE here would let the request path
-- rewrite unit_price_cents, the aggregated counts, billable_rows or the
-- fencing lease. dispatched_at is the one column the API writes after
-- creating the action; it never changes status (Codex rounds 17 and 19).
GRANT UPDATE (dispatched_at) ON contact_lookup_actions TO bridgeleads_app;
GRANT SELECT, INSERT ON contact_lookup_action_results TO bridgeleads_app;
GRANT SELECT, INSERT ON contact_lookup_action_events TO bridgeleads_app;

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
-- nts_notices (058): shared trustee-sale cache, system-written only; the app
-- reads auction data off Result columns, never this table.
REVOKE ALL ON nts_notices FROM bridgeleads_app;
-- H1 drift tables — converge to exactly the grants above:
REVOKE INSERT, DELETE ON mfa_break_glass_codes FROM bridgeleads_app;
REVOKE UPDATE, DELETE ON scraper_batches, batch_runs FROM bridgeleads_app;
REVOKE SELECT, UPDATE, DELETE ON audit_events FROM bridgeleads_app;
REVOKE INSERT, DELETE ON dialer_deliveries FROM bridgeleads_app;
-- notifications (065): app gets SELECT + UPDATE only; system writes the feed.
REVOKE INSERT, DELETE ON notifications FROM bridgeleads_app;
-- stripe_webhook_events (095): append-only ledger.
REVOKE UPDATE, DELETE ON stripe_webhook_events FROM bridgeleads_app;
-- contact_lookup_* (101): no DELETE anywhere; no UPDATE on verdicts or
-- events. The action's table-wide UPDATE revoke is NOT here: it has to run
-- before the column grant, so it lives with the grants above.
REVOKE DELETE ON contact_lookup_actions FROM bridgeleads_app;
REVOKE UPDATE, DELETE ON contact_lookup_action_results FROM bridgeleads_app;
REVOKE UPDATE, DELETE ON contact_lookup_action_events FROM bridgeleads_app;

-- Hard-fail if the app role still holds any DELETE (allowlisted exceptions:
-- mfa_backup_codes — H1 grant block; pending_registrations — verify drops the
-- address's sibling staging rows), or any write on the read-only / no-app
-- tables — so a stale over-grant cannot survive a rerun.
DO $verify$
DECLARE bad int;
BEGIN
    SELECT COUNT(*) INTO bad FROM information_schema.role_table_grants
    WHERE grantee = 'bridgeleads_app'
      AND (
        (privilege_type = 'DELETE'
            AND table_name NOT IN ('mfa_backup_codes', 'pending_registrations'))
        OR (privilege_type IN ('INSERT', 'UPDATE')
            AND table_name IN ('results', 'job_logs', 'county_records', 'referral_events',
                               'property_list_membership'))
        OR (privilege_type = 'UPDATE'
            AND table_name IN ('county_connectors', 'password_history'))
        OR table_name IN ('delivered_records', 'pending_skip_trace_rows',
                          'skip_trace_queues', 'skip_trace_cache', 'skip_trace_meter_events',
                          'nts_notices')
        -- H1 drift tables:
        OR (privilege_type = 'INSERT'
            AND table_name IN ('mfa_break_glass_codes', 'dialer_deliveries',
                               'notifications'))
        OR (privilege_type = 'UPDATE'
            AND table_name IN ('scraper_batches', 'batch_runs', 'audit_events'))
        OR (privilege_type = 'SELECT' AND table_name = 'audit_events')
        -- contact_lookup_* (101): the API may never transition a verdict or
        -- rewrite history. Its UPDATE on contact_lookup_actions is COLUMN
        -- level, which information_schema.role_table_grants does not report
        -- at all, so a table-level UPDATE appearing here means the narrow
        -- grant was replaced by a table-wide one.
        OR (privilege_type = 'UPDATE'
            AND table_name IN ('contact_lookup_actions',
                               'contact_lookup_action_results',
                               'contact_lookup_action_events'))
      );
    IF bad > 0 THEN
        RAISE EXCEPTION 'provision_rls_roles: bridgeleads_app still holds % '
            'disallowed privilege(s) — least-privilege convergence failed', bad;
    END IF;

    -- contact_lookup_actions: the check above cannot see column grants, so it
    -- passed while the API held NO update at all (Codex round 19). Ask for
    -- EFFECTIVE privilege instead: has_*_privilege takes the role explicitly
    -- and folds in PUBLIC, inheritance and table-wide grants, where
    -- information_schema.column_privileges is filtered by the CURRENT user's
    -- role membership. Exactly dispatched_at, nothing wider, nothing missing.
    IF has_table_privilege('bridgeleads_app', 'public.contact_lookup_actions', 'UPDATE') THEN
        RAISE EXCEPTION 'provision_rls_roles: bridgeleads_app holds table-wide '
            'UPDATE on contact_lookup_actions; it may update dispatched_at only';
    END IF;
    IF NOT has_column_privilege('bridgeleads_app', 'public.contact_lookup_actions',
                                'dispatched_at', 'UPDATE') THEN
        RAISE EXCEPTION 'provision_rls_roles: bridgeleads_app cannot UPDATE '
            'contact_lookup_actions.dispatched_at; the API could not record a dispatch';
    END IF;
    SELECT COUNT(*) INTO bad FROM pg_attribute
    WHERE attrelid = 'public.contact_lookup_actions'::regclass
      AND attnum > 0 AND NOT attisdropped
      AND attname <> 'dispatched_at'
      AND has_column_privilege('bridgeleads_app', 'public.contact_lookup_actions',
                               attname::text, 'UPDATE');
    IF bad > 0 THEN
        RAISE EXCEPTION 'provision_rls_roles: bridgeleads_app can UPDATE % '
            'column(s) of contact_lookup_actions besides dispatched_at', bad;
    END IF;
END
$verify$;

-- Deliberately NOT granted to bridgeleads_app (worker-only, no request-path
-- access): delivered_records, skip_trace_cache, skip_trace_queues,
-- pending_skip_trace_rows, skip_trace_meter_events. The Tracerfy webhook
-- dispatches to a Celery worker via .delay() (webhooks.py:134), so all
-- skip-trace billing writes land on bridgeleads_system, not here.

-- ✅ H1 RESOLVED (2026-06-12) — the MFA-table grant question this block used to
--   track is settled: mfa_backup_codes gets the single allowlisted app DELETE
--   (grant block above), mfa_break_glass_codes is SELECT/UPDATE only, and the
--   verify block allowlists exactly that. See tasks/todo.md (H1) for the design
--   record and the Codex consult that picked scoped-grant over SECURITY DEFINER.

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
-- tasks.py releases THIS job's cross-job dedup claims when the export upload to
-- R2 fails (no deliverable produced) so the never-delivered, unbilled leads
-- aren't treated as duplicates on a re-scrape. Without DELETE the cleanup would
-- silently fail under the cutover role and orphan the claims.
GRANT DELETE ON delivered_records TO bridgeleads_system;  -- upload-failure dedup-claim release
-- H1: operator MFA reset (scripts/reset_user_mfa.py:92, runs via railway worker)
-- physically DELETEs both MFA tables.
GRANT DELETE ON mfa_backup_codes, mfa_break_glass_codes TO bridgeleads_system;
-- pending_registrations (074): the worker dispatcher SELECTs + UPDATEs rows
-- (outbox send) and the hourly purge DELETEs expired rows. SELECT/UPDATE come
-- from the ALL TABLES grant above; DELETE is granted explicitly here.
GRANT DELETE ON pending_registrations TO bridgeleads_system;
-- skip_trace_cache: the daily Privacy Policy §7 retention sweep
-- (scheduler_helpers/retention.py) DELETEs rows past the reuse window, which hold
-- raw_response (the full Tracerfy payload). NULLing the PII columns on `results`
-- needs nothing new -- the ALL TABLES UPDATE above already covers it -- so this is
-- the only privilege the retention task adds.
GRANT DELETE ON skip_trace_cache TO bridgeleads_system;
-- contact_lookup_action_events (101): append-only for the WORKER TOO. The
-- blanket GRANT ... ON ALL TABLES above hands bridgeleads_system UPDATE on
-- every table, this one included, which would let the process that writes
-- the billing-dispute history also rewrite it. History the writer can edit
-- is not evidence (Codex). Must stay AFTER that grant to converge.
REVOKE UPDATE ON contact_lookup_action_events FROM bridgeleads_system;

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
-- bridgeleads_app DELETE rows: expect EXACTLY TWO (mfa_backup_codes — the H1
-- allowlisted exception; pending_registrations — verify drops sibling staging rows):
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
