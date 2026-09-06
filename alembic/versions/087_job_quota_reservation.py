"""Per-job quota RESERVATION — stop concurrent jobs over-allocating the same quota.

The plan cap computed a user's remaining quota with a plain read:

    remaining = records_limit - records_used
    ... mark every row past `remaining` as over-quota ...

and only charged ``records_used`` much later, in a DIFFERENT transaction, after
the export had been written and uploaded. Between those two points nothing held
the quota. Two jobs running concurrently for one user could therefore both read
``remaining = N`` and both deliver N leads, handing the user 2N records on an
N-record allowance — and the same for every child of a multi-county batch, which
fans out precisely that way.

The atomic ``records_used = records_used + :n`` increment added earlier prevents
a LOST UPDATE (the totals do sum correctly), but summing correctly is not the
same as allocating correctly: it records the over-delivery faithfully rather
than preventing it.

A lock cannot span the gap — the cap block commits before the export runs, which
releases it. So the quota is instead RESERVED at cap time, in the single atomic
statement that also computes it, and settled at billing time:

  * ``reserved_at``   — CAS gate. Only the attempt that flips it from NULL
                        reserves, so a watchdog re-run of the same job reuses
                        the existing grant instead of taking a second one.
  * ``reserved_count`` — how many records this job was granted, and therefore
                        how many to hand back if it never bills.

Billing then applies ``billable_count - reserved_count``: the delta, not the
whole amount. A job that delivers exactly what it reserved settles to zero, and
a job that never reserved (an unlimited-plan user, whose cap block is skipped)
has ``reserved_count = 0`` and so still charges its full count. One expression
covers both.

Revision ID: 087
Revises: 086
Create Date: 2026-09-05
"""

import sqlalchemy as sa
from alembic import op

revision = "087"
down_revision = "086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "reserved_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "jobs",
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=True),
    )
    # In-flight jobs at deploy time have already passed their cap block under
    # the old read-then-cap logic and will bill the full amount. reserved_count
    # = 0 makes the new settlement expression (billable - reserved) reduce to
    # the old behaviour for exactly those jobs, so the deploy is seamless.


def downgrade() -> None:
    op.drop_column("jobs", "reserved_at")
    op.drop_column("jobs", "reserved_count")
