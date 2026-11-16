"""Add trustee_sale ("Auction Leads") connector for Cowlitz County, INACTIVE.

Points at CowlitzWATrusteeSaleScraper, which reads the nts_notices rows an operator
approves from Cowlitz's OCR'd recorded Notices of Trustee's Sale
(nts_crawler.crawl_nts_cowlitz_recorder -> nts_recorder_documents ->
scripts/nts_review.py approve). Recorded Discontinuances are applied from their related
numbers by the same crawl. Coexists with Cowlitz's Laserfiche recorder row: the registry
matches by record_type.

Inserted INACTIVE: it is turned on by hand once the crawl has run and an operator has
reviewed its first notices (owner decision). health_status is seeded 'healthy' like 159
(connectors hides unknown rows). Data INSERT only, idempotent.

Revision ID: 165
Revises: 164
Create Date: 2026-11-15
"""

from alembic import op

revision = "165"
down_revision = "164"
branch_labels = None
depends_on = None

_HEALTH_STATUS = "healthy"
_COUNTY = "cowlitz"
# Reference only: the scraper reads nts_notices, never this URL.
_BASE_URL = "https://www.cowlitzinfo.net/WLAudPublic/welcome.aspx?dbid=0&repo=CCIMAGES"
_SCRAPER_CLASS = "src.scrapers.trustee_sale.CowlitzWATrusteeSaleScraper"


def upgrade() -> None:
    import uuid as _uuid

    op.execute(f"""
        INSERT INTO county_connectors (
            id, county, state, record_types, scraper_class, scraper_mode, render_mode, base_url, health_status, active
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
            '{_HEALTH_STATUS}',
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
