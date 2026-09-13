"""Add stripe_webhook_events: a durable, transactional ledger of handled Stripe events.

The webhook deduplicated on a Redis key written BEFORE the handler ran, so a
handler that raised (a Stripe read blip), a failed commit, or a process killed
mid-request (every deploy restarts the api) left Stripe retrying an event we
then skipped as "already processed". For a fully discounted checkout that event
is the only one that activates the plan.

The ledger row is inserted in the same transaction as the handler's changes,
under a transaction-scoped advisory lock on the event id, so a duplicate
delivery waits for the first to commit or roll back and then sees the truth.

Not tenant data (event id, type, time), written only by the API webhook, which
runs as bridgeleads_app with no tenant GUC. So:
  * RLS enabled, and bridgeleads_app gets separate SELECT and INSERT policies
    that admit every row (a combined "FOR SELECT, INSERT" is not valid SQL);
  * GRANT SELECT, INSERT only: nothing updates or deletes an event record;
  * PUBLIC / anon / authenticated get nothing, whatever the platform's default
    privileges granted at CREATE TABLE.
Role-guarded DO blocks keep CI / local databases (single superuser, no roles) a
clean no-op, the convention from migrations 029 and 084. Mirrored in
scripts/provision_rls_roles.sql and scripts/apply_rls_cutover_policies.sql.

Revision ID: 095
Revises: 094
Create Date: 2026-09-13
"""

import sqlalchemy as sa
from alembic import op

revision = "095"
down_revision = "094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stripe_webhook_events",
        sa.Column("event_id", sa.String(255), primary_key=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.execute("ALTER TABLE public.stripe_webhook_events ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        DO $stripe_events$
        BEGIN
            REVOKE ALL ON public.stripe_webhook_events FROM PUBLIC;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL ON public.stripe_webhook_events FROM anon;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL ON public.stripe_webhook_events FROM authenticated;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_app') THEN
                GRANT SELECT, INSERT ON public.stripe_webhook_events TO bridgeleads_app;
                DROP POLICY IF EXISTS stripe_webhook_events_app_select
                    ON public.stripe_webhook_events;
                CREATE POLICY stripe_webhook_events_app_select
                    ON public.stripe_webhook_events
                    FOR SELECT TO bridgeleads_app USING (true);
                DROP POLICY IF EXISTS stripe_webhook_events_app_insert
                    ON public.stripe_webhook_events;
                CREATE POLICY stripe_webhook_events_app_insert
                    ON public.stripe_webhook_events
                    FOR INSERT TO bridgeleads_app WITH CHECK (true);
            END IF;
        END
        $stripe_events$;
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS stripe_webhook_events_app_insert ON public.stripe_webhook_events"
    )
    op.execute(
        "DROP POLICY IF EXISTS stripe_webhook_events_app_select ON public.stripe_webhook_events"
    )
    op.drop_table("stripe_webhook_events")
