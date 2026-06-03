"""Phase 2b RLS cutover: SECURITY DEFINER helpers + public sample cache.

Three async API routes are cross-tenant and cannot set a single-user RLS GUC.
Under the future NOBYPASSRLS bridgeleads_app role they would break. Rather than
GRANT bridgeleads_system TO bridgeleads_app (which would let an internet-facing
API compromise become the worker role) or add broad policies that destroy
tenant isolation, Codex's design uses bounded primitives:

  * grant_referral_credit(referee_id)  — SECURITY DEFINER. The Stripe webhook's
    referral grant (was _grant_referral_credit in billing.py) touches TWO users
    (referrer + referee). The function, owned by the privileged migration role,
    inserts the audit row + increments the referrer's balance atomically and
    idempotently (unique(referee_id) → Stripe replay is a no-op). EXECUTE is
    granted ONLY to bridgeleads_app; REVOKEd from PUBLIC.
  * activation_funnel(days)            — SECURITY DEFINER. Admin-only analytics
    that aggregates across ALL users. Returns ONLY the funnel counts (no raw
    cross-tenant rows). The route keeps the % math in Python.
  * public_sample_cache                — a precomputed, ALREADY-SANITIZED table
    the public /scrapers/sample endpoint reads instead of live-querying tenant
    tables (results/jobs/scraper_configs). Refreshed by a Celery task. A public
    unauthenticated endpoint must never elevate or touch tenant data live.

Bug fixed in passing: the original activation-funnel query (billing.py) selected
only (id, plan, created_at) into window_users but referenced stripe_customer_id
in the paid_upgrade subquery — that column was not in the CTE, so the query would
raise at runtime (admin-only endpoint, evidently never exercised). The ported
function selects stripe_customer_id into the CTE so it actually runs.

SECURITY DEFINER notes (Codex review): functions pin search_path = public,
pg_temp, use no dynamic SQL, REVOKE EXECUTE FROM PUBLIC, and GRANT EXECUTE only
to bridgeleads_app. They are owned by the role that runs this migration (the
schema owner / current Postgres role), so they run with that role's privileges —
the controlled cross-tenant primitive the app role itself lacks.

Role-targeted GRANT/POLICY statements are wrapped in DO-blocks that no-op when
the bridgeleads_app/bridgeleads_system roles do not yet exist (CI/test DBs that
never ran scripts/provision_rls_roles.sql). The functions + table are always
created; only the role bindings are conditional. This keeps the migration green
everywhere and does not fight provision_rls_roles.sql over role passwords.

Inert under today's BYPASSRLS role. Tenant/role policies on the existing tables
land in Phase 2c (a later migration).

Revision ID: 029
Revises: 028
Create Date: 2026-06-02
"""

from alembic import op

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


_REFERRAL_BONUS_CENTS = 2000  # mirror billing.py _REFERRAL_BONUS_CENTS ($20)


def upgrade() -> None:
    # ── 1. SECURITY DEFINER: referral grant (cross-user, idempotent) ─────────
    op.execute(f"""
        CREATE OR REPLACE FUNCTION public.grant_referral_credit(p_referee_id uuid)
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $fn$
        DECLARE
            v_referrer uuid;
            v_bonus integer := {_REFERRAL_BONUS_CENTS};
            v_inserted uuid;
        BEGIN
            -- Resolve the referrer from the referee's stored relationship.
            -- Schema-qualified throughout (Codex review): defence-in-depth for
            -- a SECURITY DEFINER routine even with search_path pinned.
            SELECT referred_by_user_id INTO v_referrer
            FROM public.users WHERE id = p_referee_id;
            IF v_referrer IS NULL THEN
                RETURN;  -- organic signup, or referrer/referee deleted
            END IF;

            -- Idempotent: unique(referee_id) makes a Stripe webhook replay a
            -- no-op. RETURNING is NULL on conflict, so the balance is bumped
            -- ONLY when a brand-new event row was written.
            INSERT INTO public.referral_events
                (id, referrer_id, referee_id, amount_cents, reason, created_at)
            VALUES
                (gen_random_uuid(), v_referrer, p_referee_id, v_bonus,
                 'referee_converted_to_paid', now())
            ON CONFLICT (referee_id) DO NOTHING
            RETURNING referrer_id INTO v_inserted;

            IF v_inserted IS NOT NULL THEN
                UPDATE public.users
                SET referral_credit_cents = referral_credit_cents + v_bonus
                WHERE id = v_referrer;
            END IF;
        END;
        $fn$;
    """)
    op.execute("REVOKE ALL ON FUNCTION public.grant_referral_credit(uuid) FROM PUBLIC")

    # ── 2. SECURITY DEFINER: activation funnel (admin analytics, aggregates) ─
    # stripe_customer_id added to the CTE — see module docstring (orig bug).
    op.execute("""
        CREATE OR REPLACE FUNCTION public.activation_funnel(p_days integer)
        RETURNS TABLE (
            signups bigint,
            first_scraper bigint,
            first_job bigint,
            first_download bigint,
            paid_upgrade bigint
        )
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $fn$
            WITH window_users AS (
                SELECT id, plan, created_at, stripe_customer_id
                FROM public.users
                WHERE created_at >= NOW() - (p_days || ' days')::interval
                  AND is_active = true
            )
            SELECT
                (SELECT COUNT(*) FROM window_users),
                (SELECT COUNT(DISTINCT u.id) FROM window_users u
                   JOIN public.scraper_configs sc ON sc.user_id = u.id),
                (SELECT COUNT(DISTINCT u.id) FROM window_users u
                   JOIN public.jobs j ON j.user_id = u.id),
                (SELECT COUNT(DISTINCT u.id) FROM window_users u
                   JOIN public.jobs j ON j.user_id = u.id
                   WHERE j.export_key IS NOT NULL),
                (SELECT COUNT(*) FROM window_users
                   WHERE LOWER(plan) IN ('pro', 'business', 'agency')
                     AND stripe_customer_id IS NOT NULL);
        $fn$;
    """)
    op.execute("REVOKE ALL ON FUNCTION public.activation_funnel(integer) FROM PUBLIC")

    # Supabase quirk (Codex review): REVOKE FROM PUBLIC does NOT remove direct
    # default grants the project may hold for anon/authenticated. Explicitly
    # revoke EXECUTE from them so these definer functions cannot be invoked via
    # the anon PostgREST API. Guarded — the roles exist on Supabase, not on
    # bare-Postgres CI/test.
    op.execute("""
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL ON FUNCTION public.grant_referral_credit(uuid) FROM anon;
                REVOKE ALL ON FUNCTION public.activation_funnel(integer) FROM anon;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL ON FUNCTION public.grant_referral_credit(uuid) FROM authenticated;
                REVOKE ALL ON FUNCTION public.activation_funnel(integer) FROM authenticated;
            END IF;
        END
        $do$;
    """)

    # ── 3. Public sample cache (sanitized; refreshed by a Celery task) ───────
    # Singleton row. Stores ONLY already-sanitized data the landing page shows,
    # so RLS exposure (if any role reads it) is non-sensitive. RLS enabled to
    # match the 027 posture; bindings below let app read + system write.
    op.execute("""
        CREATE TABLE IF NOT EXISTS public.public_sample_cache (
            id smallint PRIMARY KEY DEFAULT 1,
            payload jsonb NOT NULL,
            refreshed_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT public_sample_cache_singleton CHECK (id = 1)
        )
    """)
    op.execute("ALTER TABLE public.public_sample_cache ENABLE ROW LEVEL SECURITY")

    # ── 4. Role bindings — conditional on the cutover roles existing ─────────
    # No-op on CI/test DBs that never ran provision_rls_roles.sql.
    op.execute("""
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_app') THEN
                GRANT EXECUTE ON FUNCTION public.grant_referral_credit(uuid) TO bridgeleads_app;
                GRANT EXECUTE ON FUNCTION public.activation_funnel(integer) TO bridgeleads_app;
                GRANT SELECT ON public.public_sample_cache TO bridgeleads_app;
                DROP POLICY IF EXISTS public_sample_cache_app_read ON public.public_sample_cache;
                CREATE POLICY public_sample_cache_app_read ON public.public_sample_cache
                    FOR SELECT TO bridgeleads_app USING (true);
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
                GRANT SELECT, INSERT, UPDATE ON public.public_sample_cache TO bridgeleads_system;
                DROP POLICY IF EXISTS public_sample_cache_system_all ON public.public_sample_cache;
                CREATE POLICY public_sample_cache_system_all ON public.public_sample_cache
                    FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true);
            END IF;
        END
        $do$;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.public_sample_cache")
    op.execute("DROP FUNCTION IF EXISTS public.activation_funnel(integer)")
    op.execute("DROP FUNCTION IF EXISTS public.grant_referral_credit(uuid)")
