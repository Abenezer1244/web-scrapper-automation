"""Record when the PROVIDER got the batch, so overage can be billed against it.

Automatic skip-trace overage billing has been switched off at the gate
(``USAGE_PROVENANCE_IS_TRUSTWORTHY`` in ``src/api/billing/skip_trace_usage.py``)
because nothing recorded when the lookups actually happened, and Stripe bills a
meter event into the subscription period containing its timestamp. Every column
that looked like it might answer the question was the wrong clock:

  skip_trace_meter_events.created_at   server_default=now() — the ingest
                                       reconciliation transaction.
  skip_trace_queues.completed_at       set to `now` by the ingest worker a few
                                       statements before billing runs, in the
                                       SAME transaction.
  skip_trace_queues.submitted_at       written by the dispatcher on send, which
                                       is right — but _persist_submission also
                                       runs on the reconciler's ADOPTION path,
                                       and there it inserts the queue row for
                                       the first time with `now`. For an adopted
                                       queue that is the adoption time, days
                                       after the work.

This adds a column whose provenance is fixed at the moment of writing and never
revised:

  * On the live dispatch path it is Tracerfy's own ``created_at`` when the
    response carries one, else the instant we completed the POST. Either is at
    or before the lookups — the provider cannot run them before it receives
    them — so it is a LOWER BOUND on execution.
  * On the reconciler's adoption path it is Tracerfy's ``created_at`` and
    nothing else. If Tracerfy does not tell us, this stays NULL, because the
    only other value available is the adoption clock and that is the bug.

A lower bound is the fail-safe direction, which is the whole reason this column
can be trusted where ``submitted_at`` could not: it can only push usage EARLIER,
out of a billable window (before a subscription began, or past Stripe's 35-day
backdating limit), never into one. Both of those outcomes route the row to a
human instead of charging somebody. Being wrong in this direction costs a
conversation; being wrong in the other direction costs a customer money they
never agreed to spend.

NULL keeps its meaning: "no time we can defend". ``assert_billable`` refuses it
as ``usage_at_unknown`` and the row goes to ``needs_review``. Historical rows are
NOT backfilled — their true execution time is not recoverable, and a fabricated
value would be worse than an absent one because it would look authoritative and
would be billed against.

Revision ID: 093
Revises: 092
"""
import sqlalchemy as sa
from alembic import op

revision = "093"
down_revision = "092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "skip_trace_queues",
        sa.Column("provider_submitted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("skip_trace_queues", "provider_submitted_at")
