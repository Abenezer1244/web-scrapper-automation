"""Add King County tax delinquent connector (Socrata API).

Revision ID: 013
Revises: 012
Create Date: 2026-04-09
"""

from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    import uuid as _uuid

    op.execute(f"""
        INSERT INTO county_connectors (
            id, county, state, record_types, scraper_class, scraper_mode, render_mode, base_url, health_status, active
        ) VALUES (
            '{str(_uuid.uuid4())}',
            'king',
            'wa',
            '["tax_delinquent"]',
            'src.scrapers.king_wa_tax_delinquent.KingWATaxDelinquentScraper',
            'manual',
            'static',
            'https://data.kingcounty.gov/resource/dsv3-ct3e.json',
            'unknown',
            true
        )
    """)


def downgrade() -> None:
    op.execute(
        "DELETE FROM county_connectors WHERE county = 'king' AND state = 'wa' "
        "AND scraper_class = 'src.scrapers.king_wa_tax_delinquent.KingWATaxDelinquentScraper'"
    )
