"""pacs_tax_status: per-parcel tax status for counties with no delinquent list (Asotin).

Shared, system-only table (RLS on; grant + policy for bridgeleads_system inside a
role-existence guard, exactly as 058 nts_notices), filled by workers/pacs_tax_crawler.
Also seeds Asotin's tax_delinquent connector INACTIVE: the registry and
/scrapers/connectors hide it, so public coverage does not move until release.

Revision ID: 151
Revises: 150
Create Date: 2026-11-12
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "151"
down_revision = "150"
branch_labels = None
depends_on = None

_SCRAPER_CLASS = "src.scrapers.asotin_wa_tax_delinquent.AsotinWATaxDelinquentScraper"
_BASE_URL = "https://propertysearch.trueautomation.com/PropertyAccess/?cid=10"


def upgrade() -> None:
    import uuid as _uuid

    op.create_table(
        "pacs_tax_status",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("county", sa.String(64), nullable=False),
        sa.Column("state", sa.String(4), nullable=False),
        sa.Column("parcel_id", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("as_of", sa.Date, nullable=True),
        sa.Column("delinquent_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("oldest_delinquent_year", sa.Integer, nullable=True),
        sa.Column("years_owing", sa.JSON, nullable=True),
        sa.Column("owner_name", sa.String(512), nullable=True),
        sa.Column("property_address", sa.String(512), nullable=True),
        sa.Column("mailing_address", sa.String(512), nullable=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("privacy_suppression_id", UUID(as_uuid=False),
                  sa.ForeignKey("privacy_suppressions.id", ondelete="RESTRICT"), nullable=True),
        sa.UniqueConstraint("county", "state", "parcel_id", name="uq_pacs_tax_status_parcel"),
    )
    # The crawler's round-robin and the scraper's delinquent read.
    op.create_index("ix_pacs_tax_status_attempt", "pacs_tax_status",
                    ["county", "state", "attempted_at"])
    op.create_index("ix_pacs_tax_status_owing", "pacs_tax_status", ["county", "state"],
                    postgresql_where=sa.text("delinquent_amount > 0"))

    op.execute("ALTER TABLE pacs_tax_status ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        DO $pts_rls$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON pacs_tax_status TO bridgeleads_system;
                DROP POLICY IF EXISTS pacs_tax_status_system ON pacs_tax_status;
                CREATE POLICY pacs_tax_status_system ON pacs_tax_status
                    FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true);
            END IF;
        END
        $pts_rls$;
        """
    )

    op.execute(f"""
        INSERT INTO county_connectors (
            id, county, state, record_types, scraper_class, scraper_mode, render_mode,
            base_url, assessor_url, health_status, active
        )
        SELECT '{str(_uuid.uuid4())}', 'asotin', 'wa', '["tax_delinquent"]',
               '{_SCRAPER_CLASS}', 'manual', 'static', '{_BASE_URL}', '{_BASE_URL}',
               'healthy', false
        WHERE NOT EXISTS (
            SELECT 1 FROM county_connectors
            WHERE county = 'asotin' AND state = 'wa' AND scraper_class = '{_SCRAPER_CLASS}'
        )
    """)


def downgrade() -> None:
    op.execute(f"DELETE FROM county_connectors WHERE state = 'wa' AND scraper_class = '{_SCRAPER_CLASS}'")
    op.execute("DROP POLICY IF EXISTS pacs_tax_status_system ON pacs_tax_status")
    op.drop_index("ix_pacs_tax_status_owing", table_name="pacs_tax_status")
    op.drop_index("ix_pacs_tax_status_attempt", table_name="pacs_tax_status")
    op.drop_table("pacs_tax_status")
