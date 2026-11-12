"""Clallam (WA): death_certificate on the recorder connector, and a tax_delinquent connector.

Owner approval 2026-11-12 (tasks/clallam-seven.md).

1. The Clallam EagleWeb recorder connector gains ``death_certificate`` (recorded death
   certificates, EagleWeb template). Guarded on the exact current list so re-running, or
   any hand-edited row, is left alone.
2. A dedicated tax_delinquent connector for ClallamWATaxDelinquentScraper (Treasurer
   tax-foreclosure certificate + county property portal). base_url is the Treasurer's
   stable landing page; the scraper resolves the current certificate from it at run time.
   Seeded 'unknown' like Snohomish (040), so it stays hidden until the canary verifies it.

Data only (no schema change). Idempotent.

Revision ID: 140
Revises: 139
Create Date: 2026-11-12
"""

from alembic import op

revision = "140"
down_revision = "139"
branch_labels = None
depends_on = None

_COUNTY = "clallam"
_RECORDER_HOST = "erecording.clallamcountywa.gov"
_OLD_TYPES = '["probate", "pre_foreclosure"]'
_NEW_TYPES = '["probate", "pre_foreclosure", "death_certificate"]'
_TAX_SCRAPER_CLASS = "src.scrapers.clallam_wa_tax_delinquent.ClallamWATaxDelinquentScraper"
_TAX_BASE_URL = "https://www.clallamcountywa.gov/1077/Tax-Foreclosure-Sales"
# Fixed id, so downgrade deletes only the row this revision inserted (Codex).
_TAX_CONNECTOR_ID = "7c1a1a2e-3f0c-4c6e-9f40-140000000140"


def upgrade() -> None:
    op.execute(f"""
        UPDATE county_connectors
        SET record_types = '{_NEW_TYPES}'
        WHERE lower(county) = '{_COUNTY}' AND lower(state) = 'wa'
          AND base_url ILIKE '%{_RECORDER_HOST}%'
          AND record_types::jsonb = '{_OLD_TYPES}'::jsonb
    """)
    op.execute(f"""
        INSERT INTO county_connectors (
            id, county, state, record_types, scraper_class, scraper_mode, render_mode, base_url, health_status, active
        )
        SELECT
            '{_TAX_CONNECTOR_ID}', '{_COUNTY}', 'wa', '["tax_delinquent"]', '{_TAX_SCRAPER_CLASS}',
            'manual', 'static', '{_TAX_BASE_URL}', 'unknown', true
        WHERE NOT EXISTS (
            SELECT 1 FROM county_connectors
            WHERE lower(county) = '{_COUNTY}' AND lower(state) = 'wa'
              AND scraper_class = '{_TAX_SCRAPER_CLASS}'
        )
    """)


def downgrade() -> None:
    op.execute(f"DELETE FROM county_connectors WHERE id = '{_TAX_CONNECTOR_ID}'")
    op.execute(f"""
        UPDATE county_connectors
        SET record_types = '{_OLD_TYPES}'
        WHERE lower(county) = '{_COUNTY}' AND lower(state) = 'wa'
          AND base_url ILIKE '%{_RECORDER_HOST}%'
          AND record_types::jsonb = '{_NEW_TYPES}'::jsonb
    """)
