"""Add code_violation to Pierce County connector + create separate connector for it.

Code violations use a different scraper (ArcGIS API) than ARMS portal records,
so they need a separate connector row pointing to PierceWACodeViolationScraper.

Revision ID: 011
Revises: 010
Create Date: 2026-04-08
"""

from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    import uuid as _uuid

    # Add a separate connector for Pierce code violations (different scraper class)
    op.execute(f"""
        INSERT INTO county_connectors (
            id, county, state, record_types, scraper_class, scraper_mode, render_mode, base_url, health_status, active
        ) VALUES (
            '{str(_uuid.uuid4())}',
            'pierce',
            'wa',
            '["code_violation"]',
            'src.scrapers.pierce_wa_code_violation.PierceWACodeViolationScraper',
            'manual',
            'static',
            'https://services3.arcgis.com/SCwJH1pD8WSn5T5y/arcgis/rest/services/Code%20Violations/FeatureServer',
            'unknown',
            true
        )
    """)


def downgrade() -> None:
    op.execute(
        "DELETE FROM county_connectors WHERE county = 'pierce' AND state = 'wa' "
        "AND scraper_class = 'src.scrapers.pierce_wa_code_violation.PierceWACodeViolationScraper'"
    )
