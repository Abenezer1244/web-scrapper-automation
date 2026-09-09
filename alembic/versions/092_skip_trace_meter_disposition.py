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

Backfill: classified on evidence READ WHEN THIS RUNS, not on what was true when
it was written. Unreported rows whose owner has no Stripe customer at all become
`non_billable / pre_subscription` — they cannot have had an agreement, which is
the same certainty the runtime rule demands before writing anything off.
Everything else unreported becomes `needs_review / coverage_unproven`.

An earlier draft wrote off EVERY unreported row, justified by a comment saying
this deployment had no subscriptions. That is an assertion about the past which
stops being true the moment somebody subscribes before the migration runs, and
it would have discarded their usage silently while an identical row arriving a
minute after deploy went to review. A backfill must not be more confident than
the runtime rule it precedes.

`usage_at` is left NULL throughout: it cannot be reconstructed, and a fabricated
value would be worse than an absent one because it would look authoritative and
would be billed against.

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
    # Text, not String(64). A machine reason ("pre_subscription") fits in 64
    # characters; the human one a settlement carries does not, and the failure
    # mode is the worst kind — Postgres rejects the UPDATE and rolls the
    # settlement back, so an operator is told nothing was recorded after they
    # decided it. Actor and reference are their OWN columns rather than being
    # concatenated into the reason: "who decided this" and "which invoice
    # recovered it" are the two questions asked six months later, and neither
    # should require parsing a sentence to answer.
    op.add_column(
        "skip_trace_meter_events",
        sa.Column("disposition_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "skip_trace_meter_events",
        sa.Column("disposition_actor", sa.String(128), nullable=True),
    )
    op.add_column(
        "skip_trace_meter_events",
        sa.Column("disposition_reference", sa.String(128), nullable=True),
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

    # The held backlog, classified on EVIDENCE READ AT MIGRATION TIME rather
    # than on what was true when this file was written.
    #
    # The first draft wrote every unreported row off as pre_subscription, on the
    # strength of a comment saying production had no subscriptions. That is an
    # assertion about the past that stops being true the moment someone
    # subscribes before this runs — and it would silently discard their usage,
    # while an identical row arriving a minute after deploy goes to review. A
    # backfill must not be more confident than the runtime rule it precedes.
    #
    # Write off ONLY rows whose owner has no Stripe customer at all: they cannot
    # have had an agreement, which is the same certainty the runtime rule
    # requires for a write-off.
    op.execute(
        """
        UPDATE skip_trace_meter_events e
           SET disposition = 'non_billable',
               disposition_at = NOW(),
               disposition_reason = 'pre_subscription'
          FROM users u
         WHERE u.id = e.user_id
           AND e.reported_at IS NULL
           -- Only ever classifies an UNDECIDED row. Without this a replay
           -- would overwrite settled_manual / written_off_manual and destroy
           -- the audit trail behind a human's decision (Codex).
           AND e.disposition = 'pending'
           AND (u.stripe_customer_id IS NULL OR u.stripe_customer_id = '')
        """
    )

    # Everything else unreported goes to a human. On a deployment with no
    # subscriptions this matches zero rows; on one where somebody has
    # subscribed, it is the difference between reviewing their usage and
    # throwing it away.
    op.execute(
        """
        UPDATE skip_trace_meter_events
           SET disposition = 'needs_review',
               disposition_at = NOW(),
               disposition_reason = 'coverage_unproven'
         WHERE reported_at IS NULL
           AND disposition = 'pending'
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
    op.drop_column("skip_trace_meter_events", "disposition_reference")
    op.drop_column("skip_trace_meter_events", "disposition_actor")
    op.drop_column("skip_trace_meter_events", "disposition_reason")
    op.drop_column("skip_trace_meter_events", "disposition_at")
    op.drop_column("skip_trace_meter_events", "disposition")
