"""users.first_name + users.last_name — split required name (073)

Supersedes the single optional users.name (072) with a required first + last
name (greeting uses first_name). Both encrypted at rest (EncryptedString -> Text)
like email. Nullable at the DB level — legacy rows stay NULL until they pass the
required-name gate; requiredness is enforced at the API + frontend, not a NOT NULL
constraint (an 84-row prod base is all-NULL today, so no backfill).

users.name is intentionally LEFT in place (always NULL in prod) for a rollback
window; a later cleanup migration drops it. Downgrade drops the two new columns.
"""
import sqlalchemy as sa
from alembic import op

revision = "073"
down_revision = "072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("first_name", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("last_name", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "last_name")
    op.drop_column("users", "first_name")
