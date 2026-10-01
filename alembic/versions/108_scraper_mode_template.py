"""AI-mode removal, Phase 2b: county_connectors.scraper_mode 'ai' -> 'template' (108).

'ai' never meant an LLM: it means "pick a recorder-platform template from base_url"
(src/scrapers/registry.py). The owner decided (2026-09-30) to remove the AI mode; the
stored value is renamed in three deploys so api, worker and beat, which restart at
different moments, never meet a value they cannot read:

- 2a (live before this): every reader accepts 'template' AND 'ai'.
- 2b (this): the writers store 'template'; this migration moves the rows and the
  server default (002 set it to 'ai').
- 2c (later): a straggler UPDATE, CHECK (scraper_mode IN ('template','manual')), and
  the readers drop 'ai'.

In ONE transaction: count the rows by mode (printed to the boot log), move 'ai' to
'template', set the server default, then verify BY THE OBJECTS: no 'ai' row, no mode
outside ('template','manual'), and the catalog default is 'template'. An unknown mode
aborts the migration (and so the API boot): it is data no reader understands, and
carrying it into 2c's CHECK would only move the failure.

Locks: ROW EXCLUSIVE + the updated rows (UPDATE), then ACCESS EXCLUSIVE on
county_connectors for SET DEFAULT (catalog only, no rewrite). The table holds ~30
rows. `lock_timeout = 5s` bounds the wait to ACQUIRE each lock, so a boot behind a
long reader fails fast (and the boot retries) instead of queueing every reader
behind it. Merge with the queue quiet, as always.

Idempotent: a re-run updates nothing and re-sets the same default.

Rollback: NOT "redeploy the 2a image". 2a's alembic/versions stops at 107, so its
API boot (`alembic upgrade head`) cannot place a database at 108 and start.sh refuses
to start the API. Roll back with a revert PR that KEEPS this file: the reverted code
reads both names (2a), and the rows staying 'template' is exactly what 2a reads.
downgrade() exists for test databases and restores only the server default; it does
not turn rows back into 'ai'.

Revision ID: 108
Revises: 107
Create Date: 2026-09-30
"""
from alembic import op
from sqlalchemy import text

revision = "108"
down_revision = "107"
branch_labels = None
depends_on = None

# This migration's OWN copy of the vocabulary (a migration keeps working when
# application code moves on).
_MODES = ("template", "manual")


def _column_default(conn) -> str | None:
    return conn.execute(text(
        "SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d "
        "JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum "
        "WHERE d.adrelid = 'public.county_connectors'::regclass "
        "AND a.attname = 'scraper_mode'"
    )).scalar()


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("SET LOCAL lock_timeout = '5s'"))

    before = conn.execute(text(
        "SELECT scraper_mode, count(*) FROM public.county_connectors "
        "GROUP BY scraper_mode ORDER BY scraper_mode"
    )).all()
    print(f"108: county_connectors by scraper_mode before: {dict(before)}", flush=True)

    moved = conn.execute(text(
        "UPDATE public.county_connectors SET scraper_mode = 'template' "
        "WHERE scraper_mode = 'ai'"
    )).rowcount
    conn.execute(text(
        "ALTER TABLE public.county_connectors "
        "ALTER COLUMN scraper_mode SET DEFAULT 'template'"
    ))
    print(f"108: moved {moved} connector(s) from 'ai' to 'template'", flush=True)

    unknown = conn.execute(text(
        "SELECT scraper_mode, count(*) FROM public.county_connectors "
        "WHERE scraper_mode NOT IN ('template', 'manual') GROUP BY scraper_mode"
    )).all()
    if unknown:
        raise RuntimeError(
            f"108: unknown scraper_mode value(s) {dict(unknown)}; expected only "
            f"{_MODES}. Fix the rows, then re-run."
        )
    default = _column_default(conn)
    if default != "'template'::character varying":
        raise RuntimeError(f"108: scraper_mode server default is {default!r}, not 'template'")


def downgrade() -> None:
    """Test databases only: restore 002's server default. Rows stay 'template'."""
    conn = op.get_bind()
    conn.execute(text(
        "ALTER TABLE public.county_connectors "
        "ALTER COLUMN scraper_mode SET DEFAULT 'ai'"
    ))
