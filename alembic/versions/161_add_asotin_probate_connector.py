"""Add the Asotin County probate connector, INACTIVE.

One county_connectors row for AsotinWAProbateScraper (src/scrapers/asotin_wa_probate.py),
fed by crawl_nts_nwmarket_asotin through probate_notices (160). Same shape as 150:
DB-backed, scraper_mode='manual', render_mode='static', base_url for reference only.

Seeded active=false: Asotin is not yet released, and the registry and
GET /scrapers/connectors both hide inactive rows, so the public county coverage does
not move. The owner turns it on with the county's release.

Data INSERT only. Idempotent: WHERE NOT EXISTS on (county, state, scraper_class).

Revision ID: 161
Revises: 160
Create Date: 2026-11-14
"""

from alembic import op

revision = "161"
down_revision = "160"
branch_labels = None
depends_on = None

_COUNTY = "asotin"
_BASE_URL = "https://www.nwmarket.com/marketplace/category/legal-notices"
_ASSESSOR_URL = "https://propertysearch.trueautomation.com/PropertyAccess/?cid=10"
_SCRAPER_CLASS = "src.scrapers.asotin_wa_probate.AsotinWAProbateScraper"


def upgrade() -> None:
    import uuid as _uuid

    op.execute(f"""
        INSERT INTO county_connectors (
            id, county, state, record_types, scraper_class, scraper_mode, render_mode,
            base_url, assessor_url, health_status, active
        )
        SELECT
            '{str(_uuid.uuid4())}',
            '{_COUNTY}',
            'wa',
            '["probate"]',
            '{_SCRAPER_CLASS}',
            'manual',
            'static',
            '{_BASE_URL}',
            '{_ASSESSOR_URL}',
            'healthy',
            false
        WHERE NOT EXISTS (
            SELECT 1 FROM county_connectors
            WHERE county = '{_COUNTY}' AND state = 'wa'
              AND scraper_class = '{_SCRAPER_CLASS}'
        )
    """)


def downgrade() -> None:
    op.execute(
        "DELETE FROM county_connectors WHERE state = 'wa' "
        f"AND scraper_class = '{_SCRAPER_CLASS}'"
    )
