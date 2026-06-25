"""pending_registrations: first_name/last_name nullable (076)

The verified signup flow now collects the first/last name at /auth/verify-email
(by whoever proves email control), NOT at register — so an attacker-initiated
signup can no longer set a victim-verified account's display name. The
verified-register insert therefore stops writing these columns, so they must be
nullable.

DROP-safety (Codex): this migration only DROPs the NOT NULL constraint — it does
NOT drop the columns. Dropping the columns in one step would (a) break older app
instances mid-rollout that still read/write them and (b) be non-reversible
(re-adding NOT NULL encrypted columns can't be backfilled on a populated table).
The columns are left in place, nullable and unused; a later migration may drop
them once every instance has stopped referencing them and the 24h pending
lifetime has flushed any rows that still carry values.

Additive + reversible: downgrade restores NOT NULL. That restore can fail if any
NULL rows exist (rows inserted after this migration); the hourly purge clears the
24h pending table, so run the downgrade only after that window or after deleting
NULL-name rows. Documented rather than silently coercing data.
"""
import sqlalchemy as sa
from alembic import op

revision = "076"
down_revision = "075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("pending_registrations", "first_name", existing_type=sa.Text(), nullable=True)
    op.alter_column("pending_registrations", "last_name", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    # Restores NOT NULL. Fails if NULL-name rows exist — delete them first (or
    # wait out the 24h pending TTL + purge). We do NOT fabricate placeholder
    # names, which would be silent data corruption.
    op.alter_column("pending_registrations", "first_name", existing_type=sa.Text(), nullable=False)
    op.alter_column("pending_registrations", "last_name", existing_type=sa.Text(), nullable=False)
