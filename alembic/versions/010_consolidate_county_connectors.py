"""Consolidate county connectors: one row per county with all record types.

Merges King County's 3 separate connectors (probate, pre_foreclosure, divorce)
into a single row with record_types=['probate','pre_foreclosure','death_certificate'].
Same for Pierce County's 3 connectors.

The scraper class now accepts record_type in its constructor, so one class
handles all record types for that county.

Revision ID: 010
Revises: 009
Create Date: 2026-04-08
"""

from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── King County: merge into one connector ─────────────────────────────
    # Delete the extra pre_foreclosure and divorce rows
    op.execute(
        "DELETE FROM county_connectors WHERE county = 'king' AND state = 'wa' "
        "AND scraper_class IN ("
        "'src.scrapers.king_wa_probate.KingWaPreForeclosureScraper', "
        "'src.scrapers.king_wa_probate.KingWaDivorceScraper'"
        ")"
    )

    # Update the existing probate row to include all record types
    # and point to the base class (which now handles all types via record_type param)
    op.execute("""
        UPDATE county_connectors
        SET record_types = '["probate", "pre_foreclosure", "death_certificate"]',
            scraper_class = 'src.scrapers.king_wa_probate.KingCountyLandmarkWebScraper'
        WHERE county = 'king' AND state = 'wa'
            AND scraper_class = 'src.scrapers.king_wa_probate.KingWaProbateScraper'
    """)

    # ── Pierce County: merge into one connector ───────────────────────────
    # Delete the extra pre_foreclosure and divorce rows
    op.execute(
        "DELETE FROM county_connectors WHERE county = 'pierce' AND state = 'wa' "
        "AND scraper_class IN ("
        "'src.scrapers.pierce_wa_probate.PierceWAPreForeclosureScraper', "
        "'src.scrapers.pierce_wa_probate.PierceWADivorceScraper'"
        ")"
    )

    # Update the existing probate row to include all record types
    op.execute("""
        UPDATE county_connectors
        SET record_types = '["probate", "pre_foreclosure", "divorce"]',
            scraper_class = 'src.scrapers.pierce_wa_probate.PierceWAARMSScraper'
        WHERE county = 'pierce' AND state = 'wa'
            AND scraper_class = 'src.scrapers.pierce_wa_probate.PierceWAProbateScraper'
    """)


def downgrade() -> None:
    # Restore King County to probate-only
    op.execute("""
        UPDATE county_connectors
        SET record_types = '["probate", "death_certificate"]',
            scraper_class = 'src.scrapers.king_wa_probate.KingWaProbateScraper'
        WHERE county = 'king' AND state = 'wa'
            AND scraper_class = 'src.scrapers.king_wa_probate.KingCountyLandmarkWebScraper'
    """)
    # Restore Pierce to probate-only
    op.execute("""
        UPDATE county_connectors
        SET record_types = '["probate"]',
            scraper_class = 'src.scrapers.pierce_wa_probate.PierceWAProbateScraper'
        WHERE county = 'pierce' AND state = 'wa'
            AND scraper_class = 'src.scrapers.pierce_wa_probate.PierceWAARMSScraper'
    """)
