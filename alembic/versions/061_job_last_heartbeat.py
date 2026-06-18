"""Liveness heartbeat on jobs for watchdog stuck-detection. (061)

The watchdog used to declare an ACTIVE job "stuck" purely on started_at age
(70min). That cutoff has to sit ABOVE run_scrape_job's 65min Celery hard limit
so a long-but-healthy enrich (e.g. a 24,708-parcel King tax sweep) isn't
re-queued while still working — but it also means a genuinely dead job waits the
full 70min to be recovered, and a job that legitimately needs >65min is killed
and re-queued anyway (the 2026-06-17 duplication incident).

`last_heartbeat_at` lets the watchdog re-queue an active job only once its
liveness signal is STALE (worker genuinely gone), independent of how long it has
been running. run_scrape_job writes it from a background heartbeat thread every
~60s. NULL = pre-deploy job or one that hasn't beat yet → the watchdog falls
back to the conservative started_at cutoff for those (see scheduler_helpers/health).

NULLABLE = instant ALTER on the RLS-FORCE'd jobs table (no rewrite, no policy
change). No index: the watchdog already scans jobs by status (small table).

Revision ID: 061
Revises: 060
Create Date: 2026-06-17
"""
import sqlalchemy as sa
from alembic import op

revision = "061"
down_revision = "060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "last_heartbeat_at")
