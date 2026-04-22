"""Set assessor_url on Island County connector (Tyler PACS PropertyAccess).

Why: Island probate records are estate filings (Cert of Death, Personal Rep
Deed, Transfer-on-Death) that carry no parcel in the recording index. For the
records that DO have a parcel, enrichment must know where Island's parcel
lookup lives. Island publishes a Tyler PACS PropertyAccess instance at
assessor.islandcountywa.gov/propertyaccess/ — same vendor Chelan/Douglas use.

Pairs with src/scrapers/enrichment/ai_assessor.py which will read this
via the _KNOWN_ASSESSOR_URLS dict or the explicit assessor_url param.
"""

from alembic import op

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


_ISLAND_PACS_URL = "https://assessor.islandcountywa.gov/propertyaccess/?cid=0"


def upgrade() -> None:
    op.execute(
        "UPDATE county_connectors "
        f"SET assessor_url = '{_ISLAND_PACS_URL}' "
        "WHERE LOWER(county) = 'island' AND LOWER(state) = 'wa'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE county_connectors "
        "SET assessor_url = NULL "
        "WHERE LOWER(county) = 'island' AND LOWER(state) = 'wa'"
    )
