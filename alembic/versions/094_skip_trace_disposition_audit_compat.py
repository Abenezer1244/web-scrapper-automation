"""Make the 092 audit columns true regardless of which 092 a database ran.

Migration 092 was edited after it had already been applied to some databases —
it originally wrote `disposition_reason` as VARCHAR(64) and had no
`disposition_actor` / `disposition_reference`. Alembic records 092 as done and
will never re-run it, so those databases keep the old shape while the ORM and
`scripts/settle_skip_trace_meter_rows.py` expect the new one. The symptom is not
subtle but it is late: `db.get(SkipTraceMeterEvent, ...)` selects columns that do
not exist and fails before the billing rule is even reached (Codex).

Everything here is conditional, so on a database that ran the CURRENT 092 this
is a no-op, and on one that ran the old 092 it is the missing half.

It also corrects one classification, and only one. The old 092 wrote EVERY
unreported row to `non_billable / pre_subscription` on the strength of a comment
asserting this deployment had never had a subscription. That is an assertion
about the past which stops being true the moment somebody subscribes, and
`non_billable` is a WRITE-OFF. Rows carrying that automatic reason whose owner
DOES have a Stripe customer are moved to `needs_review / coverage_unproven` —
the same place the current 092 would have put them.

Scoped tightly on purpose: only `disposition_reason = 'pre_subscription'`, which
no human ever writes (the settle script writes `settled_manual` and
`written_off_manual` with their own reasons). A person's decision is never
touched.

Revision ID: 094
Revises: 093
"""
from alembic import op

revision = "094"
down_revision = "093"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE skip_trace_meter_events
            ADD COLUMN IF NOT EXISTS disposition_actor varchar(128),
            ADD COLUMN IF NOT EXISTS disposition_reference varchar(128)
        """
    )
    # Idempotent: already text on a current-092 database.
    op.execute(
        """
        ALTER TABLE skip_trace_meter_events
            ALTER COLUMN disposition_reason TYPE text
        """
    )
    # The over-confident write-off, undone for the rows it could not have been
    # right about. Matches nothing on a database that ran the current 092.
    op.execute(
        """
        UPDATE skip_trace_meter_events e
           SET disposition = 'needs_review',
               disposition_reason = 'coverage_unproven',
               disposition_at = NOW()
          FROM users u
         WHERE u.id = e.user_id
           AND e.disposition = 'non_billable'
           AND e.disposition_reason = 'pre_subscription'
           AND e.reported_at IS NULL
           AND u.stripe_customer_id IS NOT NULL
           AND u.stripe_customer_id <> ''
        """
    )


def downgrade() -> None:
    # The columns are dropped by 092's own downgrade; re-narrowing the reason
    # would truncate settlements, and re-writing off the reclassified rows would
    # restore a decision this migration exists to retract. Deliberately empty.
    pass
