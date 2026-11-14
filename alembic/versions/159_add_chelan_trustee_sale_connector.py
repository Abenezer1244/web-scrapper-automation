"""Add trustee_sale ("Auction Leads") connector for Chelan County.

Points at ChelanWATrusteeSaleScraper, which reads the nts_notices rows an operator
approves from Chelan's OCR'd recorded Notices of Trustee's Sale
(nts_crawler.crawl_nts_chelan_recorder -> nts_recorder_documents ->
scripts/nts_review.py approve). Coexists with Chelan's AcclaimWeb recorder row: the
registry matches by record_type.

Inserted INACTIVE, and it must stay off until recorded Notices of Discontinuance are
reconciled (phase 4a-3c): until then a cancelled sale could still be delivered (Codex
P1). health_status is seeded 'healthy' like 155 (connectors hides unknown rows). Data
INSERT only, idempotent.

Revision ID: 159
Revises: 158
Create Date: 2026-11-14
"""

from alembic import op

revision = "159"
down_revision = "158"
branch_labels = None
depends_on = None

_HEALTH_STATUS = "healthy"
_COUNTY = "chelan"
# Reference only: the scraper reads nts_notices, never this URL.
_BASE_URL = "https://acclaim.co.chelan.wa.us/AcclaimWeb"
_SCRAPER_CLASS = "src.scrapers.trustee_sale.ChelanWATrusteeSaleScraper"


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
