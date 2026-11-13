"""Add the Benton County tax_delinquent connector (Treasurer foreclosure listing).

Dedicated connector, separate from Benton's EagleWeb recorder row: the registry
matches by record_type. base_url is the Treasurer's stable Foreclosure and
Distraint page, which seeds the SSRF allowlist; the scraper resolves the current
listing PDF off it at run time.

Inserted INACTIVE: the coverage page and scheduler only see it once the owner
switches it on after deploy (UPDATE county_connectors SET active = true ...).
Data INSERT only, idempotent.

Revision ID: 154
Revises: 153
Create Date: 2026-11-12
"""

from alembic import op

revision = "154"
down_revision = "153"
branch_labels = None
depends_on = None

_SCRAPER_CLASS = "src.scrapers.benton_wa_tax_delinquent.BentonWATaxDelinquentScraper"


def upgrade() -> None:
    import uuid as _uuid

    op.execute(f"""
        INSERT INTO county_connectors (
            id, county, state, record_types, scraper_class, scraper_mode, render_mode, base_url, health_status, active
        )
        SELECT
            '{str(_uuid.uuid4())}',
            'benton',
            'wa',
            '["tax_delinquent"]',
            '{_SCRAPER_CLASS}',
            'manual',
            'static',
            'https://www.bentoncountywa.gov/government/elected_officials/treasurer/foreclosure_and_distraint.php',
            'unknown',
            false
        WHERE NOT EXISTS (
            SELECT 1 FROM county_connectors
            WHERE county = 'benton' AND state = 'wa'
              AND scraper_class = '{_SCRAPER_CLASS}'
        )
    """)


def downgrade() -> None:
    op.execute(
        "DELETE FROM county_connectors WHERE county = 'benton' AND state = 'wa' "
        f"AND scraper_class = '{_SCRAPER_CLASS}'"
    )
