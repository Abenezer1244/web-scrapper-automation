"""nts_notices.issue_date: the newest newspaper issue a notice's values came from (126).

The weekly-PDF crawlers ingest the current issue, then sweep back issues. Both
upsert on (source, ts_number) and refreshed every value, so a back issue read after
a newer one put the newer one's values back to older ones: a sale postponed in last
week's issue reverted to its original date when an issue from three weeks ago was
swept (Codex, 2026-11-06). With this column an older issue can only fill gaps.

Nullable. HTML sources (one page per notice, no back issues) leave it NULL and keep
newest-read-wins. Existing PDF rows are stamped here from the issue their stored
source_url names: left NULL, the first back-issue read after deploy could still
revert one (the guard needs both dates), Codex. The parse mirrors
nts_pdf_archive.issue_date_from_url; an unparseable name stays NULL, never raises.
Additive; the running code never reads the column before this lands.
"""
import re
import urllib.parse
from datetime import date

import sqlalchemy as sa
from alembic import op

revision = "126"
down_revision = "125"
branch_labels = None
depends_on = None


_ISSUE_DATE_RE = re.compile(r"(\d{1,2})-(\d{1,2})-(\d{2})\.pdf$", re.I)


def _issue_date(url: str | None) -> date | None:
    if not url:
        return None
    name = urllib.parse.unquote(urllib.parse.urlsplit(url).path).rsplit("/", 1)[-1]
    m = _ISSUE_DATE_RE.search(name)
    if not m:
        return None
    try:
        return date(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def upgrade() -> None:
    op.add_column("nts_notices", sa.Column("issue_date", sa.Date(), nullable=True))
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, source_url FROM nts_notices WHERE source_url ILIKE '%.pdf'"
    )).all()
    stamped = [{"id": r.id, "d": d} for r in rows if (d := _issue_date(r.source_url))]
    if stamped:
        conn.execute(
            sa.text("UPDATE nts_notices SET issue_date = :d WHERE id = :id"), stamped
        )


def downgrade() -> None:
    op.drop_column("nts_notices", "issue_date")
