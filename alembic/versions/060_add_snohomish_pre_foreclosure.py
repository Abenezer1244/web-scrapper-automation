"""Add Snohomish County pre-foreclosure (Notice of Trustee Sale) connector.

Registers a dedicated pre_foreclosure connector for Snohomish, separate from the
existing Snohomish tax_delinquent connector (migration 040). The registry matches
by record_type across ALL of a county's connector rows, so two snohomish/wa rows
(tax_delinquent + pre_foreclosure) coexist cleanly — exactly as King carries
probate/code_violation/tax_delinquent.

Source = the Snohomish County Tribune weekly Legals PDF (Pacific Publishing). The
scraper (src/scrapers/snohomish_wa_pre_foreclosure.py) parses Notice-of-Trustee-Sale
leads from it, reusing the tested nts_pdf + parse_nts_notice path. base_url is the
STABLE paper legals page (the CDN PDF filename/id rotates and is discovered at run
time, never hard-coded).

Data INSERT only (no schema change). Idempotent: the WHERE NOT EXISTS is keyed on
scraper_class — which DIFFERS from the tax connector's class — so re-running is a
no-op AND the existing tax row never blocks this insert (Codex: keying on
county+state alone would see the tax row and skip the pre_foreclosure insert).

Revision ID: 060
Revises: 059
Create Date: 2026-06-14
"""

from alembic import op

revision = "060"
down_revision = "059"
branch_labels = None
depends_on = None

_SCRAPER_CLASS = (
    "src.scrapers.snohomish_wa_pre_foreclosure.SnohomishWAPreForeclosureScraper"
)
# Stable paper legals page; the weekly PDF URL is discovered off this page at run time.
_BASE_URL = "https://www.snoho.com/Content/Default/Default/Classified/Legal-Notices/-3/-3/498"


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
            '["pre_foreclosure"]',
            '{_SCRAPER_CLASS}',
            'manual',
            'static',
            '{_BASE_URL}',
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
