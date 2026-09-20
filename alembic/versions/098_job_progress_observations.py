"""jobs: durable progress OBSERVATIONS, so unknown stops rendering as zero (098).

`jobs.page_current / page_total / record_count` are `NOT NULL DEFAULT 0`, so the
row cannot tell "nothing has been measured yet" apart from "we measured, and the
answer is zero". The Live Run page therefore shows `0%`, `Records 0`, `Pages 0`
for a run that is working perfectly. Verified on job b80bd9a5 (King probate,
completed, 57 records): 401 of its 522 seconds had no status change, no log line
and no counter, because the scraper only reports after its first chunk finishes.

Every column here is NULLABLE with no default and no backfill, and NULL means
UNOBSERVED. That is the whole point: a fact is absent until somebody measures it,
and each fact is independently absent. One shared "have we started reporting yet"
flag would not do, because the scrapers report these facts at different moments —
after `on_progress(page_num, 0, n)` the completed count is known while the total
is not, and King announces its chunk total before it has found a single record.

  stage             which activity the worker is in RIGHT NOW. Free text, like
                    `jobs.trigger`, so adding one needs no migration; the set the
                    app writes lives in src/config/constants.py (JOB_STAGES) and
                    the UI copy is keyed off it. Stages REPEAT and are not a
                    linear pipeline: the CSV export runs before enrichment, and
                    scrapers do their own parcel lookup mid-scrape.
  stage_started_at  when the current stage began. Drives "still connecting to
                    King County" reassurance without re-deriving it from logs.
  records_found     RAW records the scrape returned. Deliberately NOT
                    `record_count`: that column is overwritten at `done` with the
                    BILLED non-duplicate count, which is the right number for the
                    invoice and the wrong one for "how much did we find" (57 found
                    vs 2 billed on b80bd9a5). 0 here is a real, observed zero.
  units_done        completed work units, and the denominator when one is known.
  units_total       NULL total = a genuinely unknown denominator, which is most
                    counties. No percentage may be rendered without it.
  progress_unit     what a unit IS, so the UI never calls chunks "pages": one of
                    page | chunk | parcel | record (JOB_PROGRESS_UNITS).
  last_progress_at  when a counter last moved. Distinct from last_heartbeat_at:
                    the heartbeat proves the WORKER is alive, this proves WORK is
                    advancing.
  next_retry_at     earliest time a backed-off transient retry may run. A
                    NOT-BEFORE target, never a promise: the watchdog's stranded
                    retry branch keys on created_at, so a job older than the
                    fallback cutoff can be re-delivered ahead of this (Codex).

Lock safety: nullable ADD COLUMN with no default is catalog-only (no table
rewrite), but it still takes a brief ACCESS EXCLUSIVE lock on a hot table.
lock_timeout makes the boot migration fail fast instead of queueing every job
read and write behind it. No CHECK constraints: `stage` and `progress_unit`
follow the `jobs.trigger` precedent (free text at the DB, a named set in the app)
so the next stage or unit is a code change, not a migration on a live table.

Deploy this BEFORE the worker that writes the columns. Old workers simply leave
them NULL, which reads as UNOBSERVED and is exactly right for a run they are not
reporting on.

Revision ID: 098
Revises: 097
Create Date: 2026-09-19
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "098"
down_revision = "097"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("stage", sa.String(32)),
    ("stage_started_at", sa.DateTime(timezone=True)),
    ("records_found", sa.Integer()),
    ("units_done", sa.Integer()),
    ("units_total", sa.Integer()),
    ("progress_unit", sa.String(16)),
    ("last_progress_at", sa.DateTime(timezone=True)),
    ("next_retry_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    for name, type_ in _COLUMNS:
        op.add_column("jobs", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    for name, _type in reversed(_COLUMNS):
        op.drop_column("jobs", name)
