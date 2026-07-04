"""Add trustee_sale ("Auction Leads") connector for Clark County.

Seeds one county_connectors row for Clark pointing at ClarkWATrusteeSaleScraper (the
thin per-county subclass in src/scrapers/trustee_sale.py), backed by the new NTS crawler
for The Columbian classifieds (src/workers/nts_crawler.crawl_nts_columbian_clark). The
registry matches by record_type across ALL of a county's connector rows, so this
coexists with Clark's existing recorder connector (src/scrapers/clark_wa.py) for
probate / pre_foreclosure / etc.

Same shape as migration 081 (Pierce/Snohomish/King): DB-backed (reads the shared
nts_notices cache), not a portal — scraper_mode='manual' + render_mode='static'.
base_url is the county's legal-notice listing page for reference only (the scraper never
fetches it — it queries nts_notices). health_status is seeded 'healthy' (NOT the usual
'unknown') because GET /scrapers/connectors hides unknown/down connectors: a DB-backed
connector is deterministically healthy whenever the cache has data, and the canary
re-verifies on its schedule.

Data INSERT only (no schema change). Idempotent: WHERE NOT EXISTS keyed on
(county, state, scraper_class), so re-running is a no-op and Clark's existing recorder
row never blocks the insert.

Revision ID: 082
Revises: 081
Create Date: 2026-07-04
"""

from alembic import op

revision = "082"
down_revision = "081"
branch_labels = None
depends_on = None

# Seed VISIBLE (not the usual 'unknown') — see module docstring / migration 081.
_HEALTH_STATUS = "healthy"

_COUNTY = "clark"
# base_url is the stable legal-notice source page (reference only; the scraper reads
# nts_notices, never this URL).
_BASE_URL = "https://classifieds.columbian.com/subcategories/view/55"
_SCRAPER_CLASS = "src.scrapers.trustee_sale.ClarkWATrusteeSaleScraper"


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
            true
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
