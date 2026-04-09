"""Add King County code violation connector (Seattle SDCI Socrata API).

Revision ID: 012
Revises: 011
Create Date: 2026-04-09
"""

from alembic import op

revision = "012"
down_revision = "011"
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
            '["code_violation"]',
            'src.scrapers.king_wa_code_violation.KingWACodeViolationScraper',
            'manual',
            'static',
            'https://data.seattle.gov/resource/ez4a-iug7.json',
            'unknown',
            true
        )
    """)


def downgrade() -> None:
    op.execute(
        "DELETE FROM county_connectors WHERE county = 'king' AND state = 'wa' "
        "AND scraper_class = 'src.scrapers.king_wa_code_violation.KingWACodeViolationScraper'"
    )
