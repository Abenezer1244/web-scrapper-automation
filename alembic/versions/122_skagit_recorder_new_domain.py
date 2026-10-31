"""Point Skagit (WA) connectors at the recorder's new domain.

WHY: Skagit County moved its site from skagitcounty.net to skagitcountywa.gov.
https://www.skagitcounty.net/Search/Recording/ now answers 301 to
https://classic.skagitcountywa.gov/search/Recording/ (certificate *.skagitcountywa.gov,
O=Skagit County; the same ASP.NET search form). The SSRF allowlist is built from
connector base_url hosts, so the redirect target was refused and every Skagit job and
canary failed (worker log 2026-10-31 17:20Z). Pointing base_url at the new host puts
that exact host, never the parent domain, on the allowlist; registry routes both hosts
to SkagitRecordingScraper.

Idempotent: the WHERE guard only matches the old URL (case and trailing slash
ignored), so re-running is a no-op and any other value is left alone.

Revision ID: 122
Revises: 121
Create Date: 2026-10-31
"""

from alembic import op

revision = "122"
down_revision = "121"
branch_labels = None
depends_on = None


OLD_URL = "https://www.skagitcounty.net/Search/Recording/"
NEW_URL = "https://classic.skagitcountywa.gov/search/Recording/"

# Literal SQL (no interpolation) — these URLs are static constants.
UPGRADE_SQL = """
    UPDATE county_connectors
    SET base_url = 'https://classic.skagitcountywa.gov/search/Recording/'
    WHERE lower(county) = 'skagit'
      AND lower(state) = 'wa'
      AND rtrim(lower(base_url), '/') = 'https://www.skagitcounty.net/search/recording'
"""

DOWNGRADE_SQL = """
    UPDATE county_connectors
    SET base_url = 'https://www.skagitcounty.net/Search/Recording/'
    WHERE lower(county) = 'skagit'
      AND lower(state) = 'wa'
      AND base_url = 'https://classic.skagitcountywa.gov/search/Recording/'
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
