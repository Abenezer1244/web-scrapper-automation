"""Add trustee_sale ("Auction Leads") connectors for Pierce, Snohomish, King.

Seeds one county_connectors row per county with an NTS cache source, pointing at the
thin per-county subclasses in src/scrapers/trustee_sale.py. The registry matches by
record_type across ALL of a county's connector rows, so these coexist with each
county's existing pre_foreclosure / tax_delinquent / probate rows.

trustee_sale is DB-backed (reads the shared nts_notices cache), not a portal, so like
snohomish pre_foreclosure it uses scraper_mode='manual' (honor the explicit class, not
AI template detection) + render_mode='static'. base_url is the county's stable legal-
notice source page for reference only — the scraper never fetches it (it queries
nts_notices), but county_connectors.base_url is NOT NULL.

health_status is seeded 'healthy' (NOT the usual 'unknown'): GET /scrapers/connectors
hides unknown/down connectors, so an 'unknown' seed would keep Auction Leads out of the
picker until a canary samples each row. A DB-backed connector's health is deterministic
— it works whenever the cache has data, which it does for these three crawled counties —
so 'healthy' is honest and makes the type selectable immediately after migration. The
canary re-verifies on its schedule and flips a county to degraded/down if its active
auctions empty out.

Only these three counties have an NTS crawler feeding the cache; expanding to more
counties = adding an NTS crawler per county's legal paper (see tasks/todo.md).

Data INSERT only (no schema change). Idempotent: WHERE NOT EXISTS keyed on
scraper_class (distinct per county), so re-running is a no-op and existing rows for
these counties never block the insert.

Revision ID: 081
Revises: 080
Create Date: 2026-07-03
"""

from alembic import op

revision = "081"
down_revision = "080"
branch_labels = None
depends_on = None

# Seed VISIBLE (not the usual 'unknown'): GET /scrapers/connectors hides unknown/down,
# so an unknown seed would keep Auction Leads out of the picker until a canary samples
# each row. A DB-backed connector is deterministically healthy whenever the cache has
# data (it does for these crawled counties); the canary re-verifies on its schedule.
_HEALTH_STATUS = "healthy"

# (county, base_url, scraper_class) — base_url is the stable legal-notice source page
# (reference only; the scraper reads nts_notices, never this URL).
_CONNECTORS = (
    (
        "pierce",
        "https://www.tacomadailyindex.com",
        "src.scrapers.trustee_sale.PierceWATrusteeSaleScraper",
    ),
    (
        "snohomish",
        "https://www.snoho.com/Content/Default/Default/Classified/Legal-Notices/-3/-3/498",
        "src.scrapers.trustee_sale.SnohomishWATrusteeSaleScraper",
    ),
    (
        "king",
        "https://queenannenews.com/Content/Default/Default/Classified/Legal-Notices/-3/-3/498",
        "src.scrapers.trustee_sale.KingWATrusteeSaleScraper",
    ),
)


def upgrade() -> None:
    import uuid as _uuid

    for county, base_url, scraper_class in _CONNECTORS:
        op.execute(f"""
            INSERT INTO county_connectors (
                id, county, state, record_types, scraper_class, scraper_mode, render_mode, base_url, health_status, active
            )
            SELECT
                '{str(_uuid.uuid4())}',
                '{county}',
                'wa',
                '["trustee_sale"]',
                '{scraper_class}',
                'manual',
                'static',
                '{base_url}',
                '{_HEALTH_STATUS}',
                true
            WHERE NOT EXISTS (
                SELECT 1 FROM county_connectors
                WHERE county = '{county}' AND state = 'wa'
                  AND scraper_class = '{scraper_class}'
            )
        """)


def downgrade() -> None:
    for _county, _base_url, scraper_class in _CONNECTORS:
        op.execute(
            "DELETE FROM county_connectors WHERE state = 'wa' "
            f"AND scraper_class = '{scraper_class}'"
        )
