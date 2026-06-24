"""users.name — optional encrypted display name (071)

Self-entered display name shown in the dashboard greeting (replaces the email
local-part fallback). Stored encrypted at rest (EncryptedString -> Text) to match
the email/contact-PII posture. Additive + nullable; existing rows stay NULL (no
backfill, no server_default) and the greeting falls back to no name until set.
"""
import sqlalchemy as sa
from alembic import op

revision = "071"
down_revision = "070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # EncryptedString stores Fernet tokens over Text (see src/db/encrypted_types).
    op.add_column("users", sa.Column("name", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "name")
