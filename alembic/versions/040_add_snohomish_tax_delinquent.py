"""Add Snohomish County tax delinquent connector (Treasurer bulk Current Tax List).

Registers a dedicated tax_delinquent connector for Snohomish (separate from any
recorder-based Snohomish connector — the registry matches by record_type, same
as King carries probate/code_violation/tax_delinquent rows). base_url is the
STABLE Treasurer landing page (not the monthly-rotating DocumentCenter file id):
register_connector_domains_from_db() seeds the SSRF allowlist from this host, and
the scraper resolves the current download link off this page at run time.

Data INSERT only (no schema change — the delinquent_amount/bill_year columns
already exist from migration 038). Idempotent: re-running is a no-op.

Revision ID: 040
Revises: 039
Create Date: 2026-06-05
"""

from alembic import op

revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None

_SCRAPER_CLASS = "src.scrapers.snohomish_wa_tax_delinquent.SnohomishWATaxDelinquentScraper"


def upgrade() -> None:
    import uuid as _uuid

    op.execute(f"""
        INSERT INTO county_connectors (
            id, county, state, record_types, scraper_class, scraper_mode, render_mode, base_url, health_status, active
        )
        SELECT
            '{str(_uuid.uuid4())}',
            'snohomish',
            'wa',
            '["tax_delinquent"]',
            '{_SCRAPER_CLASS}',
            'manual',
            'static',
            'https://www.snohomishcountywa.gov/5568/Treasurer-Public-Records',
            'unknown',
            true
        WHERE NOT EXISTS (
            SELECT 1 FROM county_connectors
            WHERE county = 'snohomish' AND state = 'wa'
              AND scraper_class = '{_SCRAPER_CLASS}'
        )
    """)


def downgrade() -> None:
    op.execute(
        "DELETE FROM county_connectors WHERE county = 'snohomish' AND state = 'wa' "
        f"AND scraper_class = '{_SCRAPER_CLASS}'"
    )
