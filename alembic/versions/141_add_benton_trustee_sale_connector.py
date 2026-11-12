"""Add trustee_sale ("Auction Leads") connector for Benton County.

Points at BentonWATrusteeSaleScraper, which reads nts_notices rows the Benton
recorder crawl (nts_crawler.crawl_nts_benton_recorder) builds from the county
Auditor's recorded Notices of Trustee's Sale. Coexists with Benton's EagleWeb
recorder row and its tax row: the registry matches by record_type.

Inserted INACTIVE (unlike 081/082): the owner switches it on after deploy, once
the crawl has filled the cache, so the coverage page never lists an empty source.
health_status is seeded 'healthy' like 081/082 (connectors hides unknown rows).
Data INSERT only, idempotent.

Revision ID: 141
Revises: 140
Create Date: 2026-11-12
"""

from alembic import op

revision = "141"
down_revision = "140"
branch_labels = None
depends_on = None

_HEALTH_STATUS = "healthy"
_COUNTY = "benton"
# Reference only: the scraper reads nts_notices, never this URL.
_BASE_URL = "https://erecording.co.benton.wa.us/recorder/web/"
_SCRAPER_CLASS = "src.scrapers.trustee_sale.BentonWATrusteeSaleScraper"


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
