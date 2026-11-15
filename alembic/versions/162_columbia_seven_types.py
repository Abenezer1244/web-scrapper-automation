"""Columbia (WA): recorder switched off, death_certificate on it, and the tax connector.

WHY (tasks/columbia-wa-seven.md, owner decisions 2026-11-14):
- The recorder connector is set INACTIVE. iDocMarket's Site Terms §2 prohibit
  "systematically" or "programmatically" downloading its data; access must be manual.
  It stays off until Tyler / the County give written permission; downgrade never turns
  it back on. (Production was switched off by hand the same day; this keeps it so.)
- It now collects recorded CERTIFICATE OF DEATH as death_certificate
  (IDocMarketScraper), so the connector offers it once permitted.
- Seeds the tax_delinquent connector (ColumbiaWATaxDelinquentScraper, read from the
  pacs_tax_status cache that workers/pacs_tax_crawler fills) INACTIVE: the registry
  and /scrapers/connectors hide it until an operator activates it (nothing does that
  automatically: do it once the crawl covers >= 95% of the county), as Asotin's (151).

NO assessor_url on either row, on purpose: a PACS assessor_url turns on the owner-NAME
lookup for recorder leads (enrich.py), and the owner decided Columbia never gets
addresses from a name match alone (a legal-description matcher was measured and set
aside: about 0 placements a quarter from the index's first legal). The enrichment takes the first active Columbia connector's
assessor_url, so the tax row must not carry one either. Parcel-keyed mailing for tax
leads (pacs_parcel, PACS_SITES) does not depend on it.

The recorder row was created outside migrations (production id 57cd7eed...), so a
fresh database has none and the UPDATE is then a no-op. It is matched by that id AND
its county, state and iDocMarket URL, never by URL alone. The tax row has a fixed id,
so downgrade removes only the row this migration inserted.

Idempotent. Numbered 162 on main@161 (was 158, then 160: other counties' migrations merged first).

Revision ID: 162
Revises: 161
Create Date: 2026-11-14
"""

from alembic import op

revision = "162"
down_revision = "161"
branch_labels = None
depends_on = None

_RECORDER_ID = "57cd7eed-f3db-4fca-8ee7-d4d64ce57eca"
_RECORDER_URL = "https://www.idocmarket.com/COLWA1/Document/Search"
_TAX_ID = "c0110b1a-7a40-4c6e-9f40-150000000158"
_PACS_URL = "https://propertysearch.co.columbia.wa.us/PropertyAccess/PropertySearch.aspx?cid=0"
_TAX_CLASS = "src.scrapers.columbia_wa_tax_delinquent.ColumbiaWATaxDelinquentScraper"
_RECORDER = (f"id = '{_RECORDER_ID}' AND lower(county) = 'columbia' AND lower(state) = 'wa' "
             f"AND base_url = '{_RECORDER_URL}'")


def upgrade() -> None:
    op.execute(f"UPDATE county_connectors SET active = false WHERE {_RECORDER}")
    op.execute(f"""
        UPDATE county_connectors
        SET record_types = (record_types::jsonb || '["death_certificate"]'::jsonb)::json
        WHERE {_RECORDER} AND NOT (record_types::jsonb ? 'death_certificate')
    """)
    op.execute(f"""
        INSERT INTO county_connectors (
            id, county, state, record_types, scraper_class, scraper_mode, render_mode,
            base_url, assessor_url, health_status, active
        )
        SELECT '{_TAX_ID}', 'columbia', 'wa', '["tax_delinquent"]',
               '{_TAX_CLASS}', 'manual', 'static', '{_PACS_URL}', NULL,
               'healthy', false
        WHERE NOT EXISTS (
            SELECT 1 FROM county_connectors
            WHERE county = 'columbia' AND state = 'wa' AND scraper_class = '{_TAX_CLASS}'
        )
    """)


def downgrade() -> None:
    # The recorder's `active` is left as it is: switching it on again would resume
    # access the source's terms prohibit.
    op.execute(f"DELETE FROM county_connectors WHERE id = '{_TAX_ID}'")
    op.execute(f"""
        UPDATE county_connectors
        SET record_types = (
                SELECT COALESCE(jsonb_agg(elem ORDER BY ord), '[]'::jsonb)::json
                FROM jsonb_array_elements_text(record_types::jsonb) WITH ORDINALITY AS t(elem, ord)
                WHERE elem <> 'death_certificate'
            )
        WHERE {_RECORDER}
    """)
