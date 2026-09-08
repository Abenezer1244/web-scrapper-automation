"""Funnel rates that cannot exceed 100%

`download_to_paid` divided ALL paid users in the window by observed downloaders.
Those two populations are not nested, so two paid users and one downloader
reported 200%. `job_to_download` and `scraper_to_job` have the same shape: each
count is an independent marginal, and dividing one marginal by another is only a
conversion rate when one population is provably contained in the other.

A rate like "of the people who ran a scrape, how many also downloaded" needs the
INTERSECTION, and no arithmetic on five marginal counts can recover it. So this
adds `activation_funnel_v2`, which returns the three intersections alongside the
existing counts.

A new function rather than replacing the old one, for two reasons. Postgres
refuses to change a function's return type with CREATE OR REPLACE, so adding a
column means DROP + CREATE, which silently discards the EXECUTE grant migration
029 gave bridgeleads_app. And the route selects `SELECT *`, so changing the shape
underneath a running replica is a deploy hazard. v1 stays until a later change
retires it, which also makes rollback a one-line route revert.

The counts are deliberately unchanged, so the funnel bars keep meaning exactly
what they meant yesterday. Only the ratios change, and they now measure
participation overlap rather than an ordered progression, which is all this data
can honestly support: plan, stripe_customer_id and first_leads_downloaded_at are
CURRENT values, not point-in-time, so nothing here establishes that somebody paid
AFTER downloading.

Revision ID: 091
Revises: 090
"""
from alembic import op

revision = "091"
down_revision = "090"
branch_labels = None
depends_on = None


_V2 = """
    CREATE OR REPLACE FUNCTION public.activation_funnel_v2(p_days integer)
    RETURNS TABLE (
        signups bigint,
        first_scraper bigint,
        first_job bigint,
        first_download bigint,
        paid_upgrade bigint,
        scraper_and_job bigint,
        job_and_download bigint,
        downloaded_and_paid bigint
    )
    LANGUAGE sql
    SECURITY DEFINER
    SET search_path = public, pg_temp
    AS $fn$
        -- One pass, one row per user, each stage as a flag. EXISTS rather than a
        -- JOIN: joining users to jobs multiplies a user by their job count, which
        -- is why every count here has to be over distinct users.
        WITH window_users AS (
            SELECT
                u.id,
                (LOWER(u.plan) IN ('pro', 'business', 'agency')
                    AND u.stripe_customer_id IS NOT NULL)      AS is_paid,
                (u.first_leads_downloaded_at IS NOT NULL)      AS has_download,
                EXISTS (SELECT 1 FROM public.scraper_configs sc
                         WHERE sc.user_id = u.id)              AS has_scraper,
                EXISTS (SELECT 1 FROM public.jobs j
                         WHERE j.user_id = u.id)               AS has_job
            FROM public.users u
            WHERE u.created_at >= NOW() - (p_days || ' days')::interval
              AND u.is_active = true
        )
        SELECT
            COUNT(*),
            COUNT(*) FILTER (WHERE has_scraper),
            COUNT(*) FILTER (WHERE has_job),
            COUNT(*) FILTER (WHERE has_download),
            COUNT(*) FILTER (WHERE is_paid),
            COUNT(*) FILTER (WHERE has_scraper AND has_job),
            COUNT(*) FILTER (WHERE has_job AND has_download),
            COUNT(*) FILTER (WHERE has_download AND is_paid)
        FROM window_users;
    $fn$;
"""


def _secure_v2() -> None:
    """Lock v2 down the way migration 029 locked v1 down.

    A freshly created function is EXECUTE-able by PUBLIC, and on Supabase the anon
    and authenticated roles hold default grants that REVOKE FROM PUBLIC does not
    remove. The app role then needs its grant back explicitly. All guarded, since
    a bare-Postgres CI database has none of these roles.
    """
    op.execute(
        "REVOKE ALL ON FUNCTION public.activation_funnel_v2(integer) FROM PUBLIC"
    )
    op.execute("""
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL ON FUNCTION public.activation_funnel_v2(integer) FROM anon;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL ON FUNCTION public.activation_funnel_v2(integer) FROM authenticated;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_app') THEN
                GRANT EXECUTE ON FUNCTION public.activation_funnel_v2(integer) TO bridgeleads_app;
            END IF;
        END
        $do$;
    """)


def upgrade() -> None:
    op.execute(_V2)
    _secure_v2()


def downgrade() -> None:
    # v1 was never touched, so the route reverting to it is all that rollback
    # needs. RESTRICT, not CASCADE: if something has come to depend on v2, fail
    # loudly rather than bulldoze it.
    op.execute("DROP FUNCTION IF EXISTS public.activation_funnel_v2(integer) RESTRICT")
