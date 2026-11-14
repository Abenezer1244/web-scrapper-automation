"""Columbia (WA): death_certificate + PACS owner lookup on the recorder, tax connector.

WHY (tasks/columbia-wa-seven.md, owner decisions 2026-11-14):
- The iDocMarket recorder connector now collects recorded CERTIFICATE OF DEATH as
  death_certificate (IDocMarketScraper), so the connector offers it.
- Its assessor_url was NULL, so the post-save owner-name lookup never ran for
  Columbia. It points at the county's PACS portal (pacs.ownership_year names the
  current roll; the portal cites RCW 42.56 authorization only).
- Seeds the tax_delinquent connector (ColumbiaWATaxDelinquentScraper, read from the
  pacs_tax_status cache that workers/pacs_tax_crawler fills) INACTIVE: the registry
  and /scrapers/connectors hide it until the crawl covers >= 95% of the county, as
  Asotin's (151). Same assessor_url on both rows: the name lookup takes the first
  active Columbia connector's URL, so it must not depend on which row that is.

The recorder row was created outside migrations (production id 57cd7eed...), so a
fresh database has none and the UPDATE is then a no-op. It is matched by that id AND
its county, state and iDocMarket URL, never by URL alone. The tax row has a fixed id,
so downgrade removes only the row this migration inserted.

Idempotent. Numbered 158 on main@157; renumber if another county's migration merges first.

Revision ID: 158
Revises: 157
Create Date: 2026-11-14
"""

from alembic import op

revision = "158"
down_revision = "157"
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
    op.execute(f"""
        UPDATE county_connectors
        SET record_types = CASE WHEN record_types::jsonb ? 'death_certificate' THEN record_types
                                ELSE (record_types::jsonb || '["death_certificate"]'::jsonb)::json END,
            assessor_url = '{_PACS_URL}'
        WHERE {_RECORDER}
    """)
    op.execute(f"""
        INSERT INTO county_connectors (
            id, county, state, record_types, scraper_class, scraper_mode, render_mode,
            base_url, assessor_url, health_status, active
        )
        SELECT '{_TAX_ID}', 'columbia', 'wa', '["tax_delinquent"]',
               '{_TAX_CLASS}', 'manual', 'static', '{_PACS_URL}', '{_PACS_URL}',
               'healthy', false
        WHERE NOT EXISTS (
            SELECT 1 FROM county_connectors
            WHERE county = 'columbia' AND state = 'wa' AND scraper_class = '{_TAX_CLASS}'
        )
    """)


def downgrade() -> None:
    op.execute(f"DELETE FROM county_connectors WHERE id = '{_TAX_ID}'")
    op.execute(f"""
        UPDATE county_connectors
        SET assessor_url = CASE WHEN assessor_url = '{_PACS_URL}' THEN NULL ELSE assessor_url END,
            record_types = (
                SELECT COALESCE(jsonb_agg(elem ORDER BY ord), '[]'::jsonb)::json
                FROM jsonb_array_elements_text(record_types::jsonb) WITH ORDINALITY AS t(elem, ord)
                WHERE elem <> 'death_certificate'
            )
        WHERE {_RECORDER}
    """)
