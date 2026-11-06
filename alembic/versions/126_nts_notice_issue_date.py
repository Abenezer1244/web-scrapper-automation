"""nts_notices.issue_date: the newest newspaper issue a notice's values came from (126).

The weekly-PDF crawlers ingest the current issue, then sweep back issues. Both
upsert on (source, ts_number) and refreshed every value, so a back issue read after
a newer one put the newer one's values back to older ones: a sale postponed in last
week's issue reverted to its original date when an issue from three weeks ago was
swept (Codex, 2026-11-06). With this column an older issue can only fill gaps.

Nullable, no backfill: HTML sources (one page per notice, no back issues) leave it
NULL and keep their newest-read-wins behaviour, and a PDF row gets its date the next
time an issue carrying it is read (the current issue daily, back issues within the
sweep's 7-day refresh). Additive; the running code never reads it before this lands.
"""
import sqlalchemy as sa
from alembic import op

revision = "126"
down_revision = "125"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("nts_notices", sa.Column("issue_date", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("nts_notices", "issue_date")
