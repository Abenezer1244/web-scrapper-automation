"""NTS Tier 1: nts_notices shared cache for trustee-sale auction data. (058)

A shared reference/cache table (like county_records / skip_trace_cache) — NOT
tenant-scoped. The crawler (workers/nts_crawler, system role) upserts parsed
Notice-of-Trustee-Sale notices from legal newspapers; the matcher later attaches
the auction fields onto each tenant's pre_foreclosure Results. The app never reads
this table directly (auction data reaches users via Result columns in 058+), so
it is SYSTEM-ONLY, mirroring skip_trace_cache.

RLS under the now-FORCE'd prod DB (Codex deploy-risk): this migration ENABLEs RLS
and, in a role-EXISTENCE-guarded DO-block, grants + policies the system role. It
does NOT FORCE here — FORCE makes the table owner subject to RLS too, which would
break the CI test role (no roles provisioned there) that connects as the owner.
FORCE is applied out-of-band on prod via scripts/apply_rls_force.sql (the H1
pattern), where nts_notices is now listed. In CI the guarded block no-ops and the
superuser/owner test role bypasses RLS, so inserts work.

Revision ID: 058
Revises: 057
Create Date: 2026-06-12
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "058"
down_revision = "057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "nts_notices",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),          # tacoma_daily_index
        sa.Column("ts_number", sa.String(64), nullable=False),       # trustee sale number
        sa.Column("county", sa.String(64), nullable=True),
        sa.Column("state", sa.String(2), nullable=True),
        sa.Column("parcel", sa.String(64), nullable=True),
        sa.Column("property_address", sa.String(512), nullable=True),
        # Normalized form (address_intel) — the matcher's address key.
        sa.Column("property_address_normalized", sa.String(512), nullable=True),
        sa.Column("auction_date", sa.Date(), nullable=True),
        sa.Column("auction_time", sa.String(16), nullable=True),
        sa.Column("auction_location", sa.String(512), nullable=True),
        sa.Column("grantor", sa.String(512), nullable=True),         # borrower
        sa.Column("trustee", sa.String(255), nullable=True),
        sa.Column("beneficiary", sa.String(255), nullable=True),
        sa.Column("principal_owing", sa.Numeric(12, 2), nullable=True),  # default/payoff
        sa.Column("note_amount", sa.Numeric(12, 2), nullable=True),      # original loan
        sa.Column("nod_date", sa.String(32), nullable=True),
        sa.Column("source_url", sa.String(512), nullable=True),
        # Hash of the parsed payload — dedup + detect source/parser drift for reparse.
        sa.Column("raw_hash", sa.String(64), nullable=True),
        # False once the auction is in the past / superseded (kept for audit, not matched).
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # (source, ts_number) is the natural key — NOT bare ts_number (Codex: TS#
        # collides across trustees/sources).
        sa.UniqueConstraint("source", "ts_number", name="uq_nts_notices_source_ts"),
    )
    # Matcher lookups: by normalized address, and by parcel, among active future sales.
    op.create_index(
        "ix_nts_notices_addr_active", "nts_notices",
        ["property_address_normalized"],
        postgresql_where=sa.text("is_active AND property_address_normalized IS NOT NULL"),
    )
    op.create_index(
        "ix_nts_notices_parcel_active", "nts_notices",
        ["parcel"],
        postgresql_where=sa.text("is_active AND parcel IS NOT NULL"),
    )

    # Shared system-only table: enable RLS; grant + policy the system role inside a
    # role-existence guard so CI (no roles) no-ops and prod (roles exist) converges.
    op.execute("ALTER TABLE nts_notices ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        DO $nts_rls$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON nts_notices TO bridgeleads_system;
                DROP POLICY IF EXISTS nts_notices_system ON nts_notices;
                CREATE POLICY nts_notices_system ON nts_notices
                    FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true);
            END IF;
        END
        $nts_rls$;
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS nts_notices_system ON nts_notices")
    op.drop_index("ix_nts_notices_parcel_active", table_name="nts_notices")
    op.drop_index("ix_nts_notices_addr_active", table_name="nts_notices")
    op.drop_table("nts_notices")
