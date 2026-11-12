"""Add the Asotin County trustee_sale ("Auction Leads") connector, INACTIVE.

One county_connectors row for AsotinWATrusteeSaleScraper (src/scrapers/trustee_sale.py),
fed by crawl_nts_nwmarket_asotin (Lewiston Tribune legal notices on nwmarket.com). Same
shape as 082 (Clark): DB-backed, scraper_mode='manual', render_mode='static', base_url
for reference only.

Seeded active=false on purpose: Asotin is not yet released, and the registry and
GET /scrapers/connectors both hide inactive rows, so the public county coverage stays
where it is. The owner flips it on after the crawler has filled the cache and the
release is approved. assessor_url is Asotin's PACS PropertyAccess (cid=10), read by the
owner-name PACS fallback in enrichment.

Data INSERT only. Idempotent: WHERE NOT EXISTS on (county, state, scraper_class).

Revision ID: 150
Revises: 139
Note: numbered 150+ so it cannot collide with the Benton/Chelan branches (140-142); whichever
branch merges second points its first down_revision at the other branch's head.
Create Date: 2026-11-12
"""

from alembic import op

revision = "150"
down_revision = "139"
branch_labels = None
depends_on = None

_COUNTY = "asotin"
_BASE_URL = "https://www.nwmarket.com/marketplace/category/legal-notices"
_ASSESSOR_URL = "https://propertysearch.trueautomation.com/PropertyAccess/?cid=10"
_SCRAPER_CLASS = "src.scrapers.trustee_sale.AsotinWATrusteeSaleScraper"


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
            '["trustee_sale"]',
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
