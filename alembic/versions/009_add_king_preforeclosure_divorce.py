"""Add King County pre-foreclosure connector (LandmarkWeb) + divorce placeholder.

Pre-foreclosure: "Notice of Trustee Sale" category (value=172) — active.
Divorce: NOT available on LandmarkWeb (filed at Superior Court) — inactive placeholder.

Revision ID: 009
Revises: 008
Create Date: 2026-04-08
"""

from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    import uuid as _uuid

    # King County — Pre-Foreclosure (NOD, Trustee Sale, Lis Pendens)
    op.execute(f"""
        INSERT INTO county_connectors (
            id, county, state, record_types, scraper_class, scraper_mode, render_mode, base_url, health_status, active
        ) VALUES (
            '{str(_uuid.uuid4())}',
            'king',
            'wa',
            '["pre_foreclosure"]',
            'src.scrapers.king_wa_probate.KingWaPreForeclosureScraper',
            'manual',
            'playwright',
            'https://recordsearch.kingcounty.gov/LandmarkWeb/search/index',
            'unknown',
            true
        )
    """)

    # King County — Divorce (NOT on LandmarkWeb — filed at Superior Court)
    # Inactive placeholder: divorce decrees are at dja.kingcounty.gov, not the recorder
    op.execute(f"""
        INSERT INTO county_connectors (
            id, county, state, record_types, scraper_class, scraper_mode, render_mode, base_url, health_status, active
        ) VALUES (
            '{str(_uuid.uuid4())}',
            'king',
            'wa',
            '["divorce"]',
            'src.scrapers.king_wa_probate.KingWaDivorceScraper',
            'manual',
            'playwright',
            'https://recordsearch.kingcounty.gov/LandmarkWeb/search/index',
            'unavailable',
            false
        )
    """)


def downgrade() -> None:
    op.execute(
        "DELETE FROM county_connectors WHERE county = 'king' AND state = 'wa' "
        "AND scraper_class IN ("
        "'src.scrapers.king_wa_probate.KingWaPreForeclosureScraper', "
        "'src.scrapers.king_wa_probate.KingWaDivorceScraper'"
        ")"
    )
