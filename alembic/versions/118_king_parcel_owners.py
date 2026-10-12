"""king_parcel_owners: one King owner-name answer per parcel, shared by every lead (118).

King County tax leads get their owner name only from eRealProperty, one page per
parcel: the Assessor's bulk extract redacts names (RCW 42.56.070). The same parcels
come back on every King tax run, for every account, and each run asked King again,
which is what trips King's throttle. On 2026-10-12, 22,009 of 22,855 delivered King
tax leads had no owner name and nothing was queued to fill them.

One row per parcel holds the latest settled eRealProperty answer: `found` with the
owner name, or `not_on_record` (the parcel's page names no owner). Owner recovery
fills every waiting lead of a parcel from a fresh row here before asking King.

Public-record data, not tenant data: no user_id, and nothing here says which account
asked. Filled ONLY from eRealProperty answers whose page echoed the requested parcel
(never from customers' rows), and a newer observation always wins.

Worker-only: the app role gets nothing, and the Supabase default ALL grants to anon /
authenticated / service_role are revoked. Grants/policies are role-guarded (CI has
no runtime roles) and mirrored in scripts/provision_rls_roles.sql.

Revision ID: 118
Revises: 117
Create Date: 2026-10-12
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "118"
down_revision = "117"
branch_labels = None
depends_on = None


def _guarded(role: str, stmt: str) -> str:
    return (f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN {stmt} "
            "END IF;")


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.create_table(
        "king_parcel_owners",
        # The 10-digit King PIN as text: a leading zero is part of it.
        sa.Column("parcel_id", sa.String(10), primary_key=True),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("owner_name", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.CheckConstraint("parcel_id ~ '^[0-9]{10}$'", name="ck_king_parcel_owners_pin"),
        sa.CheckConstraint("outcome IN ('found', 'not_on_record')",
                           name="ck_king_parcel_owners_outcome"),
        sa.CheckConstraint(
            "(outcome = 'found') = (owner_name IS NOT NULL AND btrim(owner_name) <> '')",
            name="ck_king_parcel_owners_name",
        ),
        sa.CheckConstraint("source IN ('erealproperty')", name="ck_king_parcel_owners_source"),
    )

    op.execute("ALTER TABLE public.king_parcel_owners ENABLE ROW LEVEL SECURITY")
    revokes = ["REVOKE ALL ON public.king_parcel_owners FROM PUBLIC;"]
    revokes += [_guarded(r, f"REVOKE ALL ON public.king_parcel_owners FROM {r};")
                for r in ("anon", "authenticated", "service_role",
                          "bridgeleads_app", "bridgeleads_system")]
    nl = "\n"
    op.execute(
        f"""
        DO $king_owner_grants$
        BEGIN
            {nl.join(revokes)}
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
                GRANT SELECT, INSERT, UPDATE ON public.king_parcel_owners TO bridgeleads_system;
                DROP POLICY IF EXISTS king_parcel_owners_system ON public.king_parcel_owners;
                CREATE POLICY king_parcel_owners_system ON public.king_parcel_owners
                    FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true);
            END IF;
        END
        $king_owner_grants$;
        """
    )


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.drop_table("king_parcel_owners")
