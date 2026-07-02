-- ============================================================================
-- apply_rls_force.sql — FORCE ROW LEVEL SECURITY (Phase 4, the LAST step)
-- ----------------------------------------------------------------------------
-- Run ONLY after provision_rls_roles.sql + apply_rls_cutover_policies.sql have
-- converged AND staging is verified healthy with RLS_ENFORCE=True. Like the
-- policy installer, this lives in an operator script (not Alembic) so it is
-- re-runnable and applied at the right cutover moment (Codex cross-phase review).
--
--   psql "$ADMIN_DATABASE_URL" -f scripts/apply_rls_force.sql
--
-- FORCE makes even the table OWNER subject to RLS. The SECURITY DEFINER helpers
-- (grant_referral_credit / activation_funnel) run as their owner — if the owner
-- lacks BYPASSRLS, FORCE would block them (no policy matches the owner role).
-- This script HARD-FAILS unless: both roles exist, the role-targeted policies
-- are present (cutover converged), and the function owners carry BYPASSRLS.
--
-- Rollback: scripts/apply_rls_force.sql is reversed by replacing FORCE with
-- `NO FORCE` — see the commented block at the bottom, or run migration 031's
-- downgrade equivalent.
-- ============================================================================

\set ON_ERROR_STOP on

DO $guard$
DECLARE
    grc regprocedure := to_regprocedure('public.grant_referral_credit(uuid)');
    af  regprocedure := to_regprocedure('public.activation_funnel(integer)');
    owners_bypass boolean;
    t text;
    tbls text[] := ARRAY[
        'scraper_configs', 'jobs', 'results', 'job_logs', 'delivered_records',
        'pending_skip_trace_rows', 'skip_trace_queues', 'password_history',
        'user_record_views', 'referral_events', 'users', 'county_connectors',
        'county_records', 'skip_trace_cache', 'skip_trace_meter_events',
        'public_sample_cache', 'property_list_membership',
        -- H1 drift tables (2026-06-12): policies in apply_rls_cutover_policies.sql
        'mfa_backup_codes', 'mfa_break_glass_codes', 'dialer_deliveries',
        'scraper_batches', 'batch_runs', 'audit_events',
        -- NTS Tier 1 (058): shared trustee-sale cache, system-only policy
        'nts_notices',
        -- Notifications (065): system-written feed, app SELECT/UPDATE policies
        'notifications',
        -- pending_registrations (074): email-verified signup staging; app + system
        -- policies in apply_rls_cutover_policies.sql
        'pending_registrations'
    ];
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_app')
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
        RAISE EXCEPTION 'apply_rls_force: both cutover roles must exist';
    END IF;

    IF grc IS NULL OR af IS NULL THEN
        RAISE EXCEPTION 'apply_rls_force: SECURITY DEFINER helpers missing — '
            'apply migration 029 first';
    END IF;

    SELECT bool_and(r.rolbypassrls) INTO owners_bypass
    FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner
    WHERE p.oid IN (grc, af);

    IF owners_bypass IS NOT TRUE THEN
        RAISE EXCEPTION 'apply_rls_force: a SECURITY DEFINER function owner lacks '
            'BYPASSRLS — FORCE would break grant_referral_credit / activation_funnel. '
            'Make the function owner a BYPASSRLS role before forcing RLS.';
    END IF;

    -- Convergence check: EVERY table to be forced must already carry its
    -- bridgeleads_system policy (apply_rls_cutover_policies.sql gives every
    -- table one). A missing one means the policy installer has not run / not
    -- converged — forcing now would default-deny that table (Codex review).
    FOREACH t IN ARRAY tbls
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'public' AND tablename = t
              AND 'bridgeleads_system' = ANY(roles)
        ) THEN
            RAISE EXCEPTION 'apply_rls_force: table % has no bridgeleads_system '
                'policy — run apply_rls_cutover_policies.sql first', t;
        END IF;
    END LOOP;

    FOREACH t IN ARRAY tbls
    LOOP
        EXECUTE format('ALTER TABLE IF EXISTS public.%I FORCE ROW LEVEL SECURITY', t);
    END LOOP;
END
$guard$;

-- ── Rollback (run manually to lift FORCE) ───────────────────────────────────
-- DO $$ DECLARE t text; BEGIN
--   FOREACH t IN ARRAY ARRAY['scraper_configs','jobs','results','job_logs',
--     'delivered_records','pending_skip_trace_rows','skip_trace_queues',
--     'password_history','user_record_views','referral_events','users',
--     'county_connectors','county_records','skip_trace_cache',
--     'skip_trace_meter_events','public_sample_cache','property_list_membership',
--     'mfa_backup_codes','mfa_break_glass_codes','dialer_deliveries',
--     'scraper_batches','batch_runs','audit_events','nts_notices',
--     'notifications','pending_registrations']
--   LOOP EXECUTE format('ALTER TABLE IF EXISTS public.%I NO FORCE ROW LEVEL SECURITY', t);
--   END LOOP; END $$;
