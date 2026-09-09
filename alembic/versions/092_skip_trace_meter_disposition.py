"""Held skip-trace usage needs a disposition, not just a reported_at

`skip_trace_meter_events` could express exactly two states: `reported_at IS NULL`
and `reported_at IS NOT NULL`. That is not enough to run this correctly, for two
separate reasons.

**`reported_at` does not mean what it says.** `report_skip_trace_meter_event`
stamps it after the try/except, which includes the `_StripeNotConfiguredError`
branch — so a row can read "reported" when no MeterEvent was ever sent. It has
also never meant "invoiced": Stripe accepts a MeterEvent for a customer with no
subscription and simply records it against the meter, where it is billed only if
some subscription item's period contains its timestamp.

**"Held" and "deliberately not billable" were the same value.** The sweep
released a held row as soon as the owner had a `stripe_customer_id`, and
`create_checkout` writes that id when the checkout SESSION is created — before
payment. So usage accrued before any subscription existed would fire against a
customer who had merely *started* checkout. Both outcomes of that are wrong: the
events land outside any billable period and are silently stranded, or (if a
subscription starts first) they are charged retroactively at a rate the customer
never agreed to. There was no way to say "we have decided this is not billable"
and no way to tell that decision apart from "not yet sent".

What this adds:

``disposition``        the actual state. `pending` is the only one the sweep
                       may touch.
``disposition_at``     when it was decided.
``disposition_reason`` why, so a human reading the table later is not guessing.
``usage_at``           when the usage HAPPENED, as opposed to when the outbox
                       row was written. `created_at` is `server_default=now()`,
                       i.e. the Postgres transaction clock during ingest
                       reconciliation, which can be long after the lookup and is
                       not a defensible thing to bill against.

Backfill: every existing unreported row becomes `non_billable /
pre_subscription`. That is not a judgement call on this deployment — there have
never been any Stripe subscriptions (0 at the time of writing, all plans set by
hand), so no held row can belong to a billing agreement that existed when the
usage was incurred. It follows that the unrecoverable true `usage_at` for those
rows cannot change their disposition either, which is why this migration does
not try to invent one.

Rows that already carry `reported_at` are marked `reported` and NOT relabelled
"billed": their provenance is ambiguous per the first paragraph, and deciding
they were invoiced would require reconciling against Stripe, which this
migration deliberately does not do.

Revision ID: 092
Revises: 091
"""
import sqlalchemy as sa
from alembic import op

revision = "092"
down_revision = "091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "skip_trace_meter_events",
        sa.Column(
            "disposition",
            sa.String(32),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "skip_trace_meter_events",
        sa.Column("disposition_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "skip_trace_meter_events",
        sa.Column("disposition_reason", sa.String(64), nullable=True),
    )
    # Nullable on purpose: it cannot be reconstructed for existing rows, and a
    # fabricated value would be worse than an absent one — it would look
    # authoritative and would be billed against. New rows set it at creation.
    op.add_column(
        "skip_trace_meter_events",
        sa.Column("usage_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Anything already sent to Stripe keeps that fact, and only that fact.
    op.execute(
        """
        UPDATE skip_trace_meter_events
           SET disposition = 'reported',
               disposition_at = reported_at
         WHERE reported_at IS NOT NULL
        """
    )

    # The held backlog. No subscription has ever existed on this deployment, so
    # none of this usage was incurred under a billing agreement.
    op.execute(
        """
        UPDATE skip_trace_meter_events
           SET disposition = 'non_billable',
               disposition_at = NOW(),
               disposition_reason = 'pre_subscription'
         WHERE reported_at IS NULL
        """
    )

    # The sweep reads exactly this. Partial, because everything else is settled
    # and the settled rows are the ones that grow without bound.
    op.create_index(
        "ix_skip_trace_meter_events_pending",
        "skip_trace_meter_events",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("disposition = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_skip_trace_meter_events_pending",
        table_name="skip_trace_meter_events",
    )
    op.drop_column("skip_trace_meter_events", "usage_at")
    op.drop_column("skip_trace_meter_events", "disposition_reason")
    op.drop_column("skip_trace_meter_events", "disposition_at")
    op.drop_column("skip_trace_meter_events", "disposition")
