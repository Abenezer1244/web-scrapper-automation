"""Add trustee_sale ("Auction Leads") connector for Douglas County, INACTIVE.

Points at DouglasWATrusteeSaleScraper, which reads the nts_notices rows
nts_crawler.crawl_nts_douglas_recorder writes from each recorded Notice of Trustee's
Sale's free detail page (the recorder's typed remarks carry the sale; the images are
sold). Recorded Discontinuances are applied by the same crawl. Coexists with Douglas's
AcclaimWeb recorder row: the registry matches by record_type.

Inserted INACTIVE: turned on by hand once the crawl has run in production and its
notices have been checked (owner decision). health_status is seeded 'healthy' like 167
(connectors hides unknown rows). Data INSERT only, idempotent.

Revision ID: 171
Revises: 170
Create Date: 2026-11-17
"""

from alembic import op

revision = "171"
down_revision = "170"
branch_labels = None
depends_on = None

_HEALTH_STATUS = "healthy"
_COUNTY = "douglas"
# Reference only: the scraper reads nts_notices, never this URL.
_BASE_URL = "https://edocs.douglascountywa.gov/AcclaimWeb"
_SCRAPER_CLASS = "src.scrapers.trustee_sale.DouglasWATrusteeSaleScraper"


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
        f"DELETE FROM county_connectors WHERE county = '{_COUNTY}' AND state = 'wa' "
        f"AND scraper_class = '{_SCRAPER_CLASS}'"
    )
