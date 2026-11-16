"""Add the Cowlitz County tax_delinquent connector (Treasurer foreclosure listing), INACTIVE.

Dedicated connector, separate from Cowlitz's Laserfiche recorder row: the registry
matches by record_type. base_url is the Treasurer's stable Foreclosure index, which seeds
the SSRF allowlist; the scraper finds the newest yearly page and its listing at run time.

Inserted INACTIVE: the owner switches it on after deploy. Its leads expire with the sale
(December 7, 2026 for the 2026 cycle). Data INSERT only, idempotent.

Revision ID: 166
Revises: 165
Create Date: 2026-11-15
"""

from alembic import op

revision = "166"
down_revision = "165"
branch_labels = None
depends_on = None

_SCRAPER_CLASS = "src.scrapers.cowlitz_wa_tax_delinquent.CowlitzWATaxDelinquentScraper"


def upgrade() -> None:
    import uuid as _uuid

    op.execute(f"""
        INSERT INTO county_connectors (
            id, county, state, record_types, scraper_class, scraper_mode, render_mode, base_url, health_status, active
        )
        SELECT
            '{str(_uuid.uuid4())}',
            'cowlitz',
            'wa',
            '["tax_delinquent"]',
            '{_SCRAPER_CLASS}',
            'manual',
            'static',
            'https://www.co.cowlitz.wa.us/1010/Foreclosure',
            'unknown',
            false
        WHERE NOT EXISTS (
            SELECT 1 FROM county_connectors
            WHERE county = 'cowlitz' AND state = 'wa'
              AND scraper_class = '{_SCRAPER_CLASS}'
        )
    """)


def downgrade() -> None:
    op.execute(
        "DELETE FROM county_connectors WHERE county = 'cowlitz' AND state = 'wa' "
        f"AND scraper_class = '{_SCRAPER_CLASS}'"
    )
