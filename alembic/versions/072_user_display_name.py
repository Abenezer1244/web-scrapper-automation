"""users.name — optional encrypted display name (072)

Self-entered display name shown in the dashboard greeting (replaces the email
local-part fallback). Stored encrypted at rest (EncryptedString -> Text) to match
the email/contact-PII posture. Additive + nullable; existing rows stay NULL (no
backfill, no server_default) and the greeting falls back to no name until set.

Renumbered 071 -> 072 (down_revision 070 -> 071): #117 and #118 both merged a
migration numbered 071 off 070 within the same second, producing a duplicate
revision id / multi-head that aborts `alembic upgrade head` at map-build (API
refuses to start). This chains the display-name column AFTER the probate
include_living_owner_tod column (071) so the chain is linear 070->071->072. The two
columns are on different tables (users vs scraper_configs) and independent, so the
ordering is arbitrary and lossless.
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
