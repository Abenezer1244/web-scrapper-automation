"""Point Columbia (WA) connector at its real iDocMarket search URL.

WHY: the Columbia connector's base_url was the iDocMarket HOMEPAGE
(https://www.idocmarket.com/), which has no search form — it routed to the
generic AIScraper and produced 0 records. Columbia's real per-county Basic
Search (public, no login/subscription needed to read the index) lives at
https://www.idocmarket.com/COLWA1/Document/Search (COLWA1 = Columbia, WA — the
only WA county on the platform). Verified live 2026-06-18: that URL returns the
recorded-document index (doc type, date, doc#, grantor/grantee, first legal).

Setting the full search URL lets registry._detect_template route Columbia to the
new IDocMarketScraper template (matches on "idocmarket.com"), replacing the
AIScraper for this county. The county code lives in the URL so the template stays
generic across iDocMarket counties.

Idempotent: the WHERE guard only updates the homepage-valued row, so re-running
is a no-op.

Revision ID: 067
Revises: 066
Create Date: 2026-06-18
"""

from alembic import op

revision = "067"
down_revision = "066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Literal SQL (no interpolation) — these URLs are static constants.
    op.execute(
        """
        UPDATE county_connectors
        SET base_url = 'https://www.idocmarket.com/COLWA1/Document/Search'
        WHERE lower(county) = 'columbia'
          AND lower(state) = 'wa'
          AND base_url IN ('https://www.idocmarket.com/', 'https://www.idocmarket.com')
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE county_connectors
        SET base_url = 'https://www.idocmarket.com/'
        WHERE lower(county) = 'columbia'
          AND lower(state) = 'wa'
          AND base_url = 'https://www.idocmarket.com/COLWA1/Document/Search'
        """
    )
