"""Add gis_endpoint and assessor_url columns to county_connectors

These columns store free GIS REST API endpoints and assessor website URLs
for the new cost-free enrichment pipeline.

Revision ID: 003
Revises: 002
Create Date: 2026-03-20
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # GIS REST API endpoint (e.g. ArcGIS FeatureServer query URL)
    op.add_column(
        "county_connectors",
        sa.Column("gis_endpoint", sa.Text(), nullable=True),
    )
    # County assessor/property search website URL (for AI scraper fallback)
    op.add_column(
        "county_connectors",
        sa.Column("assessor_url", sa.Text(), nullable=True),
    )

    # Populate Pierce County with known endpoints
    op.execute(
        "UPDATE county_connectors SET "
        "gis_endpoint = 'https://services2.arcgis.com/1UvBaQ5y1ubjUPmd"
        "/arcgis/rest/services/Tax_Parcels/FeatureServer/0/query', "
        "assessor_url = 'https://atip.piercecountywa.gov/app/parcelSearch' "
        "WHERE LOWER(county) = 'pierce' AND UPPER(state) = 'WA'"
    )


def downgrade() -> None:
    op.drop_column("county_connectors", "assessor_url")
    op.drop_column("county_connectors", "gis_endpoint")
