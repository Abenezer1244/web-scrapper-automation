"""jobs: the run-count breakdown, frozen when the run finishes (106).

A finished run showed numbers that disagreed: `records_found` 265, the worker log's
"12 new leads, 252 duplicates" (264), the results page's 258. Each was counted at a
different point over a different set of rows, and nothing named the rows in between.

These six columns partition what the run found, first match wins per saved row:

  breakdown_dropped_before_save  records_found minus the rows saved (the living-TOD
                                 filter and the save-time fingerprint merge)
  breakdown_no_address           saved, no usable property or mailing address
  breakdown_same_run_merged      combined into another row of this run
  breakdown_already_delivered    an earlier run of the account holds the claim
  breakdown_over_quota           past the plan cap
  breakdown_new                  delivered and billed; equals jobs.billed_count

They sum to records_found. The worker writes them in the done-CAS, from the same
statement that produces the billed count, so the headline, the bill and the
breakdown are one reading. Post-completion backfills change the LIVE counts on the
results page, never these.

All nullable, no default, no backfill: NULL means no snapshot (a job finished before
this migration, a retried run, or one whose numbers did not reconcile), and the API
reports the live breakdown for those instead. No CHECK constraints, following 099: a
constraint on this hot table is another migration to change.

Lock safety: nullable ADD COLUMN with no default is catalog-only (no rewrite), but it
still takes a brief ACCESS EXCLUSIVE lock on `jobs`. lock_timeout makes the boot
migration fail fast instead of queueing every job read and write behind it.

Forward-only in production: rolling back is a code revert (old code ignores the
columns). downgrade() drops the snapshots and exists for test databases.

Revision ID: 106
Revises: 105
Create Date: 2026-09-28
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision = "106"
down_revision = "105"
branch_labels = None
depends_on = None

_COLUMNS = (
    "breakdown_dropped_before_save",
    "breakdown_no_address",
    "breakdown_same_run_merged",
    "breakdown_already_delivered",
    "breakdown_over_quota",
    "breakdown_new",
)


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    for name in _COLUMNS:
        op.add_column("jobs", sa.Column(name, sa.Integer(), nullable=True))


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    for name in reversed(_COLUMNS):
        op.drop_column("jobs", name)
