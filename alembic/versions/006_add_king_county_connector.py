"""Add King, Clark, Snohomish county death certificate connectors (LandmarkWeb).

Revision ID: 006
Revises: 005
Create Date: 2026-04-05
"""

from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    import uuid as _uuid

    # King County — recordsearch.kingcounty.gov
    op.execute(f"""
        INSERT INTO county_connectors (
            id, county, state, record_types, scraper_class, render_mode, base_url, health_status, active
        ) VALUES (
            '{str(_uuid.uuid4())}',
            'king',
            'wa',
            '["probate"]',
            'src.scrapers.king_wa_probate.KingWaProbateScraper',
            'playwright',
            'https://recordsearch.kingcounty.gov/LandmarkWeb/search/index',
            'unknown',
            true
        )
    """)

    # Clark County — e-docs.clark.wa.gov (different LandmarkWeb config — needs testing)
    op.execute(f"""
        INSERT INTO county_connectors (
            id, county, state, record_types, scraper_class, render_mode, base_url, health_status, active
        ) VALUES (
            '{str(_uuid.uuid4())}',
            'clark',
            'wa',
            '["probate"]',
            'src.scrapers.king_wa_probate.ClarkWaProbateScraper',
            'playwright',
            'https://e-docs.clark.wa.gov/LandmarkWeb/search/index',
            'unknown',
            false
        )
    """)

    # Snohomish County — snoco.org/RecordedDocuments (requires login — inactive)
    op.execute(f"""
        INSERT INTO county_connectors (
            id, county, state, record_types, scraper_class, render_mode, base_url, health_status, active
        ) VALUES (
            '{str(_uuid.uuid4())}',
            'snohomish',
            'wa',
            '["probate"]',
            'src.scrapers.king_wa_probate.SnohomishWaProbateScraper',
            'playwright',
            'https://www.snoco.org/RecordedDocuments/',
            'unavailable',
            false
        )
    """)


def downgrade() -> None:
    op.execute("DELETE FROM county_connectors WHERE county IN ('king', 'clark', 'snohomish') AND state = 'wa'")
