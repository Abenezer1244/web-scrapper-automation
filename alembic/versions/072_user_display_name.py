"""users.name — optional encrypted display name (072)

Self-entered display name shown in the dashboard greeting (replaces the email
local-part fallback). Stored encrypted at rest (EncryptedString -> Text) to match
the email/contact-PII posture. Additive + nullable; existing rows stay NULL (no
backfill, no server_default) and the greeting falls back to no name until set.

NOTE: numbered 072 (down_revision 071) to avoid colliding with the parallel PR
#117 migration 071 (scraper_configs.include_living_owner_tod), which also chains
off 070. This branch must be rebased onto a main that contains 071 before merge.
"""
import sqlalchemy as sa
from alembic import op

revision = "072"
down_revision = "071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # EncryptedString stores Fernet tokens over Text (see src/db/encrypted_types).
    op.add_column("users", sa.Column("name", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "name")
