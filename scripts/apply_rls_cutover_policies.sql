-- ============================================================================
-- apply_rls_cutover_policies.sql — install role-targeted RLS policies (Phase 2c)
-- ----------------------------------------------------------------------------
-- CUTOVER STEP — run AFTER scripts/provision_rls_roles.sql (roles must exist).
-- This is the authoritative installer for the role-targeted policies. It does
-- NOT live in an Alembic migration on purpose: `alembic upgrade head` runs on
-- every deploy, and a role-conditional migration would no-op (roles absent) and
-- advance the version, never re-running when the roles are later provisioned
-- (Codex cross-phase review). Migrations 030/031 are therefore no-op
-- placeholders; this script + apply_rls_force.sql do the real work, idempotently.
--
-- Idempotent (DROP POLICY IF EXISTS + CREATE; GRANT is idempotent) and wrapped
-- in one transaction. HARD-FAILS unless BOTH cutover roles exist.
--
--   psql "$ADMIN_DATABASE_URL" -f scripts/apply_rls_cutover_policies.sql
--
-- Model: bridgeleads_app = per-tenant (app.current_user_id GUC), broad only on
-- users + shared catalogs; bridgeleads_system = FOR ALL (trusted cross-tenant).
-- anon/authenticated get NO policy → default-denied (027 lockout preserved).
-- Grants (provision_rls_roles.sql) and policies are kept ALIGNED: every app
-- policy here corresponds to an app GRANT there, and worker-only tables get a
-- system policy with no app policy.
-- ============================================================================

\set ON_ERROR_STOP on

DO $guard$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_app')
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
        RAISE EXCEPTION 'apply_rls_cutover_policies: bridgeleads_app and '
            'bridgeleads_system must both exist — run provision_rls_roles.sql first';
    END IF;
END
$guard$;

BEGIN;

-- ── 029 binding backfill (idempotent; authoritative regardless of when the
--    roles were provisioned relative to migration 029) ─────────────────────
GRANT EXECUTE ON FUNCTION public.grant_referral_credit(uuid) TO bridgeleads_app;
GRANT EXECUTE ON FUNCTION public.activation_funnel(integer)  TO bridgeleads_app;
GRANT SELECT ON public.public_sample_cache TO bridgeleads_app;
GRANT SELECT, INSERT, UPDATE ON public.public_sample_cache TO bridgeleads_system;

-- ── Tenant tables the app READS+WRITES → app FOR ALL (USING+WITH CHECK) ─────
--    (no DELETE granted to app, so FOR ALL still can't delete)
DO $tenant_rw$
DECLARE
    t text;
    guc text := 'user_id = NULLIF(current_setting(''app.current_user_id'', true), '''')::uuid';
BEGIN
    FOREACH t IN ARRAY ARRAY['scraper_configs', 'jobs', 'user_record_views']
    LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', t || '_user_isolation', t);
        EXECUTE format('DROP POLICY IF EXISTS urv_user_only ON public.%I', t);  -- 023 legacy name
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', t || '_app', t);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I TO bridgeleads_app USING (%s) WITH CHECK (%s)',
            t || '_app', t, guc, guc);
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', t || '_system', t);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true)',
            t || '_system', t);
    END LOOP;
END
$tenant_rw$;

-- ── password_history: app SELECT (reuse-check) + INSERT (append); never UPDATE.
--    Rows are immutable audit history. system FOR ALL. ────────────────────────
DROP POLICY IF EXISTS password_history_user_isolation ON public.password_history;
DROP POLICY IF EXISTS password_history_app ON public.password_history;
DROP POLICY IF EXISTS password_history_app_select ON public.password_history;
DROP POLICY IF EXISTS password_history_app_insert ON public.password_history;
CREATE POLICY password_history_app_select ON public.password_history
    FOR SELECT TO bridgeleads_app
    USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
CREATE POLICY password_history_app_insert ON public.password_history
    FOR INSERT TO bridgeleads_app
    WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
DROP POLICY IF EXISTS password_history_system ON public.password_history;
CREATE POLICY password_history_system ON public.password_history
    FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true);

-- ── Tenant table the app only READS → app FOR SELECT ────────────────────────
DROP POLICY IF EXISTS results_user_isolation ON public.results;
DROP POLICY IF EXISTS results_app ON public.results;
CREATE POLICY results_app ON public.results
    FOR SELECT TO bridgeleads_app
    USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
DROP POLICY IF EXISTS results_system ON public.results;
CREATE POLICY results_system ON public.results
    FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true);

-- ── property_list_membership: app only READS (Phase 3 overlap rollup) → app
--    FOR SELECT; system FOR ALL (worker rollup + retention prune). Modeled on
--    results (app-readable tenant table), per migration 034. ──────────────────
DROP POLICY IF EXISTS property_list_membership_user_isolation ON public.property_list_membership;
DROP POLICY IF EXISTS property_list_membership_app ON public.property_list_membership;
CREATE POLICY property_list_membership_app ON public.property_list_membership
    FOR SELECT TO bridgeleads_app
    USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
DROP POLICY IF EXISTS property_list_membership_system ON public.property_list_membership;
CREATE POLICY property_list_membership_system ON public.property_list_membership
    FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true);

-- ── job_logs: app FOR SELECT via parent job; system FOR ALL ─────────────────
DROP POLICY IF EXISTS job_logs_via_job ON public.job_logs;
DROP POLICY IF EXISTS job_logs_app ON public.job_logs;
CREATE POLICY job_logs_app ON public.job_logs
    FOR SELECT TO bridgeleads_app
    USING (job_id IN (
        SELECT id FROM public.jobs
        WHERE user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid));
DROP POLICY IF EXISTS job_logs_system ON public.job_logs;
CREATE POLICY job_logs_system ON public.job_logs
    FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true);

-- ── referral_events: app SELECT-only (writes via definer fn); system FOR ALL ─
DROP POLICY IF EXISTS referral_events_user_isolation ON public.referral_events;
DROP POLICY IF EXISTS referral_events_app ON public.referral_events;
CREATE POLICY referral_events_app ON public.referral_events
    FOR SELECT TO bridgeleads_app
    USING (referrer_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
           OR referee_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
DROP POLICY IF EXISTS referral_events_system ON public.referral_events;
CREATE POLICY referral_events_system ON public.referral_events
    FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true);

-- ── users: broad app (auth queries users pre-GUC); system FOR ALL ───────────
DROP POLICY IF EXISTS users_app ON public.users;
CREATE POLICY users_app ON public.users
    FOR ALL TO bridgeleads_app USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS users_system ON public.users;
CREATE POLICY users_system ON public.users
    FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true);

-- ── county_connectors: app SELECT + INSERT (POST /connectors); system FOR ALL ─
DROP POLICY IF EXISTS county_connectors_app_read ON public.county_connectors;
CREATE POLICY county_connectors_app_read ON public.county_connectors
    FOR SELECT TO bridgeleads_app USING (true);
DROP POLICY IF EXISTS county_connectors_app_insert ON public.county_connectors;
CREATE POLICY county_connectors_app_insert ON public.county_connectors
    FOR INSERT TO bridgeleads_app WITH CHECK (true);
DROP POLICY IF EXISTS county_connectors_system ON public.county_connectors;
CREATE POLICY county_connectors_system ON public.county_connectors
    FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true);

-- ── county_records: app shared-read (GUC present); system FOR ALL (trigger
--    still blocks GUC-scoped writes) ──────────────────────────────────────────
DROP POLICY IF EXISTS county_records_shared_read ON public.county_records;
DROP POLICY IF EXISTS county_records_app_read ON public.county_records;
CREATE POLICY county_records_app_read ON public.county_records
    FOR SELECT TO bridgeleads_app
    USING (NULLIF(current_setting('app.current_user_id', true), '') IS NOT NULL);
DROP POLICY IF EXISTS county_records_system ON public.county_records;
CREATE POLICY county_records_system ON public.county_records
    FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true);

-- ── public_sample_cache: app SELECT; system FOR ALL ─────────────────────────
DROP POLICY IF EXISTS public_sample_cache_app_read ON public.public_sample_cache;
CREATE POLICY public_sample_cache_app_read ON public.public_sample_cache
    FOR SELECT TO bridgeleads_app USING (true);
DROP POLICY IF EXISTS public_sample_cache_system_all ON public.public_sample_cache;
CREATE POLICY public_sample_cache_system_all ON public.public_sample_cache
    FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true);

-- ── Worker-only tables: system FOR ALL, NO app policy (app has no grant) ────
--    delivered_records / pending_skip_trace_rows / skip_trace_queues are
--    written ONLY by workers (dedup, skip-trace pipeline); the app never reads
--    or writes them, so they get a system policy and NO app policy — keeping
--    policy and grant aligned (Codex cross-phase review).
DO $sys_only$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'delivered_records', 'pending_skip_trace_rows', 'skip_trace_queues',
        'skip_trace_cache', 'skip_trace_meter_events'
    ]
    LOOP
        -- Drop any legacy untargeted tenant policy from 018.
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', t || '_user_isolation', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', t || '_system', t);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true)',
            t || '_system', t);
    END LOOP;
END
$sys_only$;

COMMIT;

-- ── Verification (informational) ────────────────────────────────────────────
-- Every app policy should have a matching app GRANT and vice-versa. Spot-check:
SELECT schemaname, tablename, policyname, roles, cmd
FROM pg_policies
WHERE schemaname = 'public'
  AND (policyname LIKE '%_app%' OR policyname LIKE '%_system%'
       OR policyname IN ('county_connectors_app_read', 'county_connectors_app_insert',
                         'county_records_app_read', 'public_sample_cache_app_read'))
ORDER BY tablename, policyname;
