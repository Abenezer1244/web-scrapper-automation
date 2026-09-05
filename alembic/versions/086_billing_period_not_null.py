"""Quota-loss fix: billing period columns become NOT NULL with a server default.

``users.records_period_start`` was added nullable by migration 020, which
backfilled only the rows that existed AT THAT MOMENT. No server_default was set
and the sole user-creation site never populated the column, so every user
registered after 020 carried ``records_period_start = NULL``.

The daily quota rollover (``reset_monthly_usage``) matched those rows with its
``records_period_start IS NULL`` arm and set ``records_used = 0`` — wiping a
brand-new user's consumption inside their own signup month, with no billing
event and no log of the loss. In production one account billed 999 records on
its first day and read 2 the following morning.

This migration closes the hole at the schema level:

  1. ADOPT any remaining NULL rows by stamping the current month start WITHOUT
     touching the counters. Zeroing is the financially destructive direction (it
     grants free quota and lets a user exceed their cap invisibly), so a row we
     are unsure about must never lose its counter here.
  2. Add a server_default so an insert that omits the column still gets a
     correct period.
  3. Set NOT NULL, because a server_default alone does not stop an explicit
     NULL — which is exactly how the ORM was writing these rows.

``skip_trace_period_start`` gets the same treatment: the rollover used to gate
the skip-trace reset on ``records_period_start``, so drift between the two
columns could reset Stripe-metered skip-trace usage early or never at all.

This migration does NOT repair counters that were already wiped. That is done
separately by ``scripts/repair_records_used_from_ledger.py``, which recomputes
from the durable per-job billing anchor (``jobs.billed_count`` /
``jobs.billing_applied_at``) rather than guessing.

Revision ID: 086
Revises: 085
Create Date: 2026-09-05
"""

from alembic import op

revision = "086"
down_revision = "085"
branch_labels = None
depends_on = None

_MONTH_START = "date_trunc('month', NOW() AT TIME ZONE 'UTC')"


def upgrade() -> None:
    # 1. Adopt stragglers — stamp the period, PRESERVE the counters.
    op.execute(f"""
        UPDATE users
        SET records_period_start = {_MONTH_START}
        WHERE records_period_start IS NULL
    """)
    op.execute(f"""
        UPDATE users
        SET skip_trace_period_start = {_MONTH_START}
        WHERE skip_trace_period_start IS NULL
    """)

    # 2 + 3. Default for omitted inserts, NOT NULL for explicit ones.
    for column in ("records_period_start", "skip_trace_period_start"):
        op.execute(
            f"ALTER TABLE users ALTER COLUMN {column} SET DEFAULT {_MONTH_START}"
        )
        op.execute(f"ALTER TABLE users ALTER COLUMN {column} SET NOT NULL")


def downgrade() -> None:
    for column in ("records_period_start", "skip_trace_period_start"):
        op.execute(f"ALTER TABLE users ALTER COLUMN {column} DROP NOT NULL")
        op.execute(f"ALTER TABLE users ALTER COLUMN {column} DROP DEFAULT")
