"""Measure real leads downloads instead of "an export exists"

Three places asked "has this user downloaded their leads?" and all three answered
with ``jobs.export_key IS NOT NULL``. The worker writes export_key when it marks a
job DONE, before anybody downloads anything, so the answer was really "an export
was produced". Consequences: the onboarding checklist ticked "Download leads" for
users who had never downloaded, its download step was unreachable on a normal
success, the day-3 activation email judged those users activated, and this
migration's own ``activation_funnel`` reported a job_to_download conversion of
~100% by construction, which is precisely the dropoff the funnel exists to show.

Adds two columns, deliberately kept apart:

``users.first_leads_downloaded_at``  what we OBSERVED. Written only by
    src/api/download_tracking.py when a CSV is actually handed over. NOT
    backfilled: nothing before this migration was measured, and inventing a
    timestamp would both fabricate funnel history and permanently prevent the
    row from recording its real first download.

``users.onboarding_download_grandfathered``  what we ASSUME, for presentation.
    Set here for users who already had a finished export, so their checklist does
    not regress from 5/5 and re-nag them. The funnel must never read it.

Revision ID: 089
Revises: 088
"""
import sqlalchemy as sa
from alembic import op

revision = "089"
down_revision = "088"
branch_labels = None
depends_on = None


# The pre-089 body, restored verbatim on downgrade so rolling back does not leave
# the function referencing a column this migration just dropped.
_FUNNEL_BODY_EXPORT_KEY = """
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
"""

# first_download now counts users we watched receive a CSV. Cohorts that signed up
# before this migration read as not-downloaded until they age out of the window,
# because they were never instrumented. That under-counts for one window length;
# the alternative over-counted forever.
_FUNNEL_BODY_OBSERVED = """
            WITH window_users AS (
                SELECT id, plan, created_at, stripe_customer_id,
                       first_leads_downloaded_at
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
                (SELECT COUNT(*) FROM window_users
                   WHERE first_leads_downloaded_at IS NOT NULL),
                (SELECT COUNT(*) FROM window_users
                   WHERE LOWER(plan) IN ('pro', 'business', 'agency')
                     AND stripe_customer_id IS NOT NULL);
"""


def _replace_funnel(body: str) -> None:
    """Re-create activation_funnel with `body`, keeping its security posture.

    CREATE OR REPLACE keeps existing grants, but the definer/search_path settings
    are part of the definition and must be restated, and migration 029's explicit
    revokes are re-issued so a replace can never widen access.
    """
    op.execute(f"""
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
        AS $fn${body}$fn$;
    """)
    op.execute("REVOKE ALL ON FUNCTION public.activation_funnel(integer) FROM PUBLIC")
    # Supabase holds default grants for these roles that REVOKE FROM PUBLIC does
    # not touch (migration 029). Guarded: the roles exist on Supabase, not on
    # bare-Postgres CI.
    op.execute("""
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL ON FUNCTION public.activation_funnel(integer) FROM anon;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL ON FUNCTION public.activation_funnel(integer) FROM authenticated;
            END IF;
        END
        $do$;
    """)


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("first_leads_downloaded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "onboarding_download_grandfathered",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Presentation grandfather only. Bounded to `users`, which is small, and it
    # runs in the same transaction as the ALTERs above by design: the checklist
    # must never be observed in the state where the column exists but nobody is
    # grandfathered. `jobs` is NOT rewritten, so no lock is held over a large table.
    op.execute("""
        UPDATE public.users u
        SET onboarding_download_grandfathered = true
        WHERE EXISTS (
            SELECT 1 FROM public.jobs j
            WHERE j.user_id = u.id
              AND j.status = 'done'
              AND j.export_key IS NOT NULL
        )
    """)

    _replace_funnel(_FUNNEL_BODY_OBSERVED)


def downgrade() -> None:
    # Restore the old body FIRST: it reads jobs.export_key, so it stays valid once
    # the columns below are gone. Dropping first would leave a broken function.
    _replace_funnel(_FUNNEL_BODY_EXPORT_KEY)
    op.drop_column("users", "onboarding_download_grandfathered")
    op.drop_column("users", "first_leads_downloaded_at")
