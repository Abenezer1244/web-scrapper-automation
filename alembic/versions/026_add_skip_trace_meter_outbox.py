"""Transactional outbox for Stripe skip-trace MeterEvents.

REDTEAM (Codex convergence — meter outbox): the Tracerfy ingest worker
advances each user's skip_trace_used_this_month counter and flips the
SkipTraceQueue to "completed" in ONE committed transaction (the ingest
replay-idempotency anchor). The Stripe MeterEvent was then fired AFTER that
commit through a fire-and-forget Celery enqueue whose inner Stripe call
swallowed every exception (so transient Stripe failures were never retried)
and whose enqueue itself was best-effort (so a broker-down-at-enqueue lost
the event with no record to recover from). Either gap left usage advanced
locally but never billed — permanent under-billing with no recovery anchor.

This table is the durable outbox. report_usage_from_webhook INSERTs one row
per billable user in the SAME transaction that advances the counter, so the
intent-to-bill commits atomically with the usage. A row stays reported_at =
NULL until report_skip_trace_meter_event fires the Stripe MeterEvent and
stamps reported_at. flush_skip_trace_meter_outbox (Celery beat) sweeps any
rows whose inline enqueue was lost.

Idempotency: UNIQUE(tracerfy_queue_id, user_id) + ON CONFLICT DO NOTHING on
insert; stable (queue_id, user_id) Stripe identifier on report. The partial
index on reported_at IS NULL keeps the outbox sweep cheap as the table grows.

System table — written only by workers via system_sync_session(). Like
skip_trace_queues (migration 014) and county_records (migration 023) it
carries NO user RLS policy; nothing user-scoped ever reads or writes it.

Revision ID: 026
Revises: 025
Create Date: 2026-06-01
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotency guard: if the table already exists (e.g. it was created
    # out-of-band before this revision was stamped — which happened during a
    # prod RLS cutover where alembic_version was stamped behind the live
    # schema), treat the migration as already applied. This whole revision is
    # additive for one table + its indexes, so an existing table means done.
    if sa.inspect(op.get_bind()).has_table("skip_trace_meter_events"):
        return
    op.create_table(
        "skip_trace_meter_events",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("tracerfy_queue_id", sa.Integer(), nullable=False),
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("billable_units", sa.Integer(), nullable=False),
        sa.Column("stripe_customer_id", sa.String(255), nullable=True),
        sa.Column("plan", sa.String(32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
        # Idempotency anchor: re-running report_usage_from_webhook for the same
        # batch can never duplicate an outbox row (INSERT ... ON CONFLICT DO
        # NOTHING relies on this constraint).
        sa.UniqueConstraint(
            "tracerfy_queue_id",
            "user_id",
            name="uq_skip_trace_meter_events_queue_user",
        ),
    )
    op.create_index(
        "ix_skip_trace_meter_events_tracerfy_queue_id",
        "skip_trace_meter_events",
        ["tracerfy_queue_id"],
    )
    # Partial index: the beat sweep only ever scans un-reported rows, so index
    # just those. Reported rows (the vast majority over time) stay out of it.
    op.create_index(
        "ix_skip_trace_meter_events_unreported",
        "skip_trace_meter_events",
        ["reported_at"],
        postgresql_where=sa.text("reported_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_skip_trace_meter_events_unreported",
        table_name="skip_trace_meter_events",
    )
    op.drop_index(
        "ix_skip_trace_meter_events_tracerfy_queue_id",
        table_name="skip_trace_meter_events",
    )
    op.drop_table("skip_trace_meter_events")
