"""Add external_source_health — durable health state for third-party enrichment sources.

Context: a bulk owner-name backfill got our production IP rate-blocked by King
County's eRealProperty. A per-run circuit breaker aborts the run that notices,
but breaker state dies with the process — the next worker, scheduled job or
re-run immediately starts hammering the same blocked source. This table is the
shared, durable "do not call" flag every caller consults.

Deliberately NOT reusing county_connectors.health_status: that tracks county
SCRAPER TARGETS (is Pierce's portal up?). This tracks EXTERNAL ENRICHMENT
SERVICES we call during a job (eRealProperty, ArcGIS, Tracerfy). Different
lifecycle, different canary, different remediation.

Durable rather than a Redis TTL because the cooldown after a block is measured in
DAYS, a Redis flush must never silently un-block a source, and ops needs to see
why and since when.

One row per source globally — the block is on our egress IP, not on a tenant, so
this is NOT user-scoped and carries no RLS policy (SYSTEM-written, mirroring
batch_runs / audit_events).

Schema only; seeds nothing. A source is 'healthy' by absence — no row means never
had a problem, which keeps the write path off the hot path entirely.

Revision ID: 083
Revises: 082
Create Date: 2026-07-30
"""

import sqlalchemy as sa
from alembic import op

revision = "083"
down_revision = "082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "external_source_health",
        sa.Column("source_key", sa.String(64), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="healthy"),
        sa.Column("reason", sa.String(512), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_probe_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "consecutive_probe_failures", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # The canary asks "which sources are due a probe?" — status + cooldown_until.
    op.create_index(
        "ix_external_source_health_status_cooldown",
        "external_source_health",
        ["status", "cooldown_until"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_external_source_health_status_cooldown", table_name="external_source_health"
    )
    op.drop_table("external_source_health")
