"""Marketing email suppressions: addresses that opted out of commercial email (124).

One row per email address that clicked "unsubscribe" on a commercial BridgeLeads email
(the welcome, the day-1 and day-3 nudges, the trial-ending upsell). Design and Codex
consult: tasks/todo-compliance-audit-2026-11.md, Phase 4 (audit 2026-11 P1-5, CAN-SPAM).
Schema only: the writer (the unsubscribe route) and the readers (the senders) follow.

  Keyed by the address's email_hmac (the same keyed blind index as users.email_hmac),
  NOT by user id: an opt-out must survive an account deletion (the purge rewrites the
  users row's email) and a later re-registration with the same address, and must not
  follow a user to a NEW address after an email change. Only the HMAC is stored, never
  the address.

  Kept indefinitely: it is the record that we must not send to this address. Nobody
  holds UPDATE or DELETE, and a trigger refuses UPDATE for every role (BLD23), so an
  opt-out can only be added, never changed or quietly lifted. A re-subscribe flow,
  if ever built, is a separate decision.

  Not tenant data (no user_id): like pending_registrations, the app role's policy is
  broad and the grants do the limiting. The app may INSERT (email_hmac, source) and
  SELECT; `opted_out_at` is the DATABASE clock. The worker only SELECTs (send-time
  check).

Supabase default privileges grant ALL on every new table to anon / authenticated /
service_role; revoked here. Grants/policies are role-guarded (CI has no runtime roles)
and mirrored in scripts/provision_rls_roles.sql.

Revision ID: 124
Revises: 123
Create Date: 2026-11-04
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "124"
down_revision = "123"
branch_labels = None
depends_on = None


def _guarded(role: str, stmt: str) -> str:
    return (f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN {stmt} "
            "END IF;")


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.create_table(
        "marketing_email_suppressions",
        # blind_index() output: 64 lowercase hex characters (HMAC-SHA256).
        sa.Column("email_hmac", sa.String(64), primary_key=True),
        # How the opt-out arrived: the footer link's confirm page, a mail client's
        # RFC 8058 one-click POST, or support acting on a request.
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column(
            "opted_out_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint(
            "email_hmac ~ '^[0-9a-f]{64}$'", name="ck_marketing_suppressions_hmac"
        ),
        sa.CheckConstraint(
            "source IN ('link', 'one_click', 'support')",
            name="ck_marketing_suppressions_source",
        ),
    )
    op.execute(
        """
        CREATE FUNCTION public.marketing_email_suppressions_immutable() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $fn$
        BEGIN
            RAISE EXCEPTION 'marketing_email_suppressions rows are append-only'
                USING ERRCODE = 'BLD23';
        END
        $fn$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION public.marketing_email_suppressions_immutable() FROM PUBLIC"
    )
    op.execute(
        "CREATE TRIGGER marketing_email_suppressions_no_update BEFORE UPDATE "
        "ON public.marketing_email_suppressions FOR EACH ROW "
        "EXECUTE FUNCTION public.marketing_email_suppressions_immutable()"
    )
    op.execute(
        "ALTER TABLE public.marketing_email_suppressions "
        "ENABLE ALWAYS TRIGGER marketing_email_suppressions_no_update"
    )

    op.execute("ALTER TABLE public.marketing_email_suppressions ENABLE ROW LEVEL SECURITY")
    revokes = ["REVOKE ALL ON public.marketing_email_suppressions FROM PUBLIC;"]
    revokes += [_guarded(r, f"REVOKE ALL ON public.marketing_email_suppressions FROM {r};")
                for r in ("anon", "authenticated", "service_role",
                          "bridgeleads_app", "bridgeleads_system")]
    nl = "\n"
    op.execute(
        f"""
        DO $suppression_grants$
        BEGIN
            {nl.join(revokes)}
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_app') THEN
                GRANT SELECT ON public.marketing_email_suppressions TO bridgeleads_app;
                GRANT INSERT (email_hmac, source)
                    ON public.marketing_email_suppressions TO bridgeleads_app;
                DROP POLICY IF EXISTS marketing_email_suppressions_app
                    ON public.marketing_email_suppressions;
                CREATE POLICY marketing_email_suppressions_app
                    ON public.marketing_email_suppressions
                    FOR ALL TO bridgeleads_app USING (true) WITH CHECK (true);
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
                GRANT SELECT ON public.marketing_email_suppressions TO bridgeleads_system;
                DROP POLICY IF EXISTS marketing_email_suppressions_system
                    ON public.marketing_email_suppressions;
                CREATE POLICY marketing_email_suppressions_system
                    ON public.marketing_email_suppressions
                    FOR SELECT TO bridgeleads_system USING (true);
            END IF;
        END
        $suppression_grants$;
        """
    )


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.drop_table("marketing_email_suppressions")
    op.execute("DROP FUNCTION IF EXISTS public.marketing_email_suppressions_immutable()")
