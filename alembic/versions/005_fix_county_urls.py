"""Fix county connector URLs for Lincoln, Stevens (Tyler Self-Service)."""

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None

# URL updates for counties that migrated DNS or were incorrect
_URL_FIXES = [
    # Lincoln: migrated from selfservice.co.lincoln.wa.us to tylerhost.net
    (
        "lincoln",
        "WA",
        "https://lincolncountywa-web.tylerhost.net/web/user/disclaimer",
    ),
    # Stevens: migrated from selfservice.co.stevens.wa.us to .gov
    (
        "stevens",
        "WA",
        "https://selfservice.stevenscountywa.gov/web",
    ),
    # Yakima: old Tapestry URL replaced with free AVA portal (no login required)
    (
        "yakima",
        "WA",
        "https://ava.fidlar.com/WAYakima/AvaWeb/#/search",
    ),
]


def upgrade() -> None:
    for county, state, new_url in _URL_FIXES:
        op.execute(
            f"UPDATE county_connectors "
            f"SET base_url = '{new_url}' "
            f"WHERE LOWER(county) = '{county.lower()}' "
            f"AND LOWER(state) = '{state.lower()}'"
        )


def downgrade() -> None:
    pass  # URLs were broken before, no sensible rollback
