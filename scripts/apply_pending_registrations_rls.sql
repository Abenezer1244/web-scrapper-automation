-- ============================================================================
-- apply_pending_registrations_rls.sql — grants + role-targeted RLS policies for
-- public.pending_registrations (migration 074's table).
-- ----------------------------------------------------------------------------
-- WHY THIS EXISTS: migration 074 created pending_registrations with RLS ENABLED
-- but NO policy, and no GRANTs to the runtime roles. The cutover is already DONE
-- (the app runs as the NOBYPASSRLS role bridgeleads_app, the worker as
-- bridgeleads_system), so the verified-registration INSERT hit
-- `permission denied for table pending_registrations` (a 500). This installs the
-- missing grants + policies, mirroring the users_app/users_system pattern in
-- apply_rls_cutover_policies.sql. pending_registrations is pre-account /
-- non-tenant (no user_id), so — like users — the app policy is broad USING(true).
--
-- Roles & verbs (kept aligned, grant <-> policy):
--   bridgeleads_app  (api: register INSERT, verify SELECT + sibling DELETE)
--   bridgeleads_system (worker: dispatcher SELECT/UPDATE, hourly purge DELETE)
--
-- Idempotent; run as the owner/admin (postgres / DATABASE_URL_MIGRATE).
--   psql "$DATABASE_URL_MIGRATE" -f scripts/apply_pending_registrations_rls.sql
-- ============================================================================

DO $guard$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_app')
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
        RAISE EXCEPTION 'bridgeleads_app and bridgeleads_system must both exist';
    END IF;
END
$guard$;

BEGIN;

-- Grants (GRANT is idempotent). app: no UPDATE; system: no INSERT.
GRANT SELECT, INSERT, DELETE ON public.pending_registrations TO bridgeleads_app;
GRANT SELECT, UPDATE, DELETE ON public.pending_registrations TO bridgeleads_system;

-- app: broad pre-account access (no user_id to scope on), mirrors users_app.
-- The missing UPDATE grant means FOR ALL still cannot UPDATE (grant-gated).
DROP POLICY IF EXISTS pending_registrations_app ON public.pending_registrations;
CREATE POLICY pending_registrations_app ON public.pending_registrations
    FOR ALL TO bridgeleads_app USING (true) WITH CHECK (true);

-- system: trusted cross-tenant worker (dispatch + purge). No INSERT grant.
DROP POLICY IF EXISTS pending_registrations_system ON public.pending_registrations;
CREATE POLICY pending_registrations_system ON public.pending_registrations
    FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true);

COMMIT;
