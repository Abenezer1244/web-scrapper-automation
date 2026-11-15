"""Add the Ferry County connectors (tax_delinquent, probate, divorce), all INACTIVE.

- tax_delinquent: FerryWATaxDelinquentScraper reads pacs_tax_status, which
  workers/pacs_tax_crawler fills from Ferry's TaxSifter Treasurer pages. DB-backed like
  Asotin's (151): scraper_mode='manual', render_mode='static'.
- probate, divorce: FerryWAProbateScraper / FerryWADivorceScraper read the State
  Archives Superior Court docket index in a browser (render_mode='playwright'). The
  base_url registers digitalarchives.wa.gov on the scrape allowlist.

All three rows are seeded active=false: Ferry is not released, the registry and
GET /scrapers/connectors hide inactive rows, so public coverage does not move.
probate/divorce stay off until counsel clears GR 31(g)(4) (tasks/ferry-wa-seven.md);
tax_delinquent until the crawl covers >=95% of Ferry's parcels.

Data INSERT only. Idempotent: WHERE NOT EXISTS on (county, state, scraper_class).

Revision ID: 163
Revises: 162
Create Date: 2026-11-15
"""

from alembic import op

revision = "163"
down_revision = "162"
branch_labels = None
depends_on = None

_TAXSIFTER = "https://ferrywa-taxsifter.publicaccessnow.com"
_ARCHIVES = "https://digitalarchives.wa.gov/Collections/TitleInfo/3007"
# Fixed ids: downgrade removes exactly the rows this migration inserted (Codex).
_ROWS = (
    # (id, record_types, scraper_class, render_mode, base_url)
    ("f3e0a7c1-0019-4d63-9a01-000000000163", '["tax_delinquent"]',
     "src.scrapers.ferry_wa_tax_delinquent.FerryWATaxDelinquentScraper", "static", _TAXSIFTER),
    ("f3e0a7c1-0019-4d63-9a02-000000000163", '["probate"]',
     "src.scrapers.ferry_wa_court_index.FerryWAProbateScraper", "playwright", _ARCHIVES),
    ("f3e0a7c1-0019-4d63-9a03-000000000163", '["divorce"]',
     "src.scrapers.ferry_wa_court_index.FerryWADivorceScraper", "playwright", _ARCHIVES),
)


def upgrade() -> None:
    for row_id, record_types, scraper_class, render_mode, base_url in _ROWS:
        op.execute(f"""
            INSERT INTO county_connectors (
                id, county, state, record_types, scraper_class, scraper_mode, render_mode,
                base_url, assessor_url, health_status, active
            )
            SELECT '{row_id}', 'ferry', 'wa', '{record_types}',
                   '{scraper_class}', 'manual', '{render_mode}', '{base_url}',
                   '{_TAXSIFTER}', 'unknown', false
            WHERE NOT EXISTS (
                SELECT 1 FROM county_connectors
                WHERE county = 'ferry' AND state = 'wa'
                  AND scraper_class = '{scraper_class}'
            )
        """)


def downgrade() -> None:
    for row_id, *_ in _ROWS:
        op.execute(
            "DELETE FROM county_connectors WHERE county = 'ferry' AND state = 'wa' "
            f"AND id = '{row_id}'"
        )
