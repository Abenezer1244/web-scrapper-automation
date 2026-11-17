"""Add the Asotin County lis pendens connector (pre_foreclosure), INACTIVE.

One county_connectors row for AsotinWARecorderScraper (src/scrapers/asotin_wa_recorder.py):
the county recorder's Helion portal, behind a reCAPTCHA solved through 2Captcha (owner
decision 2026-11-16). Lis pendens only: the portal's death certificates name no parcel
and gave no lead (the class can read them, but no connector offers them), and no
divorce decree was recorded in 2025 or 2026.

Seeded active=false: the owner turns it on. The legacy Asotin row (state 'WA',
['probate', 'pre_foreclosure'], no scraper class, inactive) is left as it is: the
registry reads active rows only, so it never answers for pre_foreclosure while off.

Data INSERT only. Idempotent: WHERE NOT EXISTS on (county, state, scraper_class, the
record type); the downgrade deletes exactly that row.

Revision ID: 169
Revises: 168
Create Date: 2026-11-16
"""

from alembic import op

revision = "169"
down_revision = "168"
branch_labels = None
depends_on = None

_COUNTY = "asotin"
_RECORD_TYPES = '["pre_foreclosure"]'
_BASE_URL = "https://pras.asotincountywa.gov/DigitalResearchRoom/"
_ASSESSOR_URL = "https://propertysearch.trueautomation.com/PropertyAccess/?cid=10"
_SCRAPER_CLASS = "src.scrapers.asotin_wa_recorder.AsotinWARecorderScraper"
_IDENTITY = (f"county = '{_COUNTY}' AND state = 'wa' AND scraper_class = '{_SCRAPER_CLASS}' "
             f"AND record_types::jsonb = '{_RECORD_TYPES}'::jsonb")


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
            '{_RECORD_TYPES}',
            '{_SCRAPER_CLASS}',
            'manual',
            'playwright',
            '{_BASE_URL}',
            '{_ASSESSOR_URL}',
            'healthy',
            false
        WHERE NOT EXISTS (SELECT 1 FROM county_connectors WHERE {_IDENTITY})
    """)


def downgrade() -> None:
    op.execute(f"DELETE FROM county_connectors WHERE {_IDENTITY}")
