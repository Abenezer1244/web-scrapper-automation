"""HIGH-2 (step 1/2): rewrite RLS policies with a system-role escape + WITH CHECK.

Part of the role-downgrade migration (docs/security/HIGH-2-rls-migration-plan.md).
This step is SAFE TO AUTO-APPLY: under the current BYPASSRLS role the policies are
inert, and the `current_user = 'bridgeleads_system'` term is just a string compare
(the role need not exist yet). It does NOT enable FORCE and does NOT change the DB
role — those are the deliberate manual cutover (step 2/2), see
docs/security/high-2-cutover-runbook.sql.

What changes vs migration 018/023:
- Every policy gains `OR current_user = 'bridgeleads_system'` so the dedicated
  cross-tenant worker role can read/write across tenants once the role swap
  happens (the API/app role gets no such escape — it stays user-scoped).
- Every policy gains an explicit WITH CHECK (was USING-only) so a write can't set
  a row to another user's id once FORCE RLS is enabled.

Revision ID: 025
Revises: 024
Create Date: 2026-06-01
"""
from alembic import op

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None

_USER = "user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid"
_SYS = "current_user = 'bridgeleads_system'"
_JOBLOGS = (
    "job_id IN (SELECT id FROM jobs WHERE user_id = "
    "NULLIF(current_setting('app.current_user_id', true), '')::uuid)"
)
_REF = (
    "referrer_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid "
    "OR referee_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid"
)

# (table, policy, predicate, for_all)
_SIMPLE = [
    ("scraper_configs", "scraper_configs_user_isolation", _USER, False),
    ("jobs", "jobs_user_isolation", _USER, False),
    ("results", "results_user_isolation", _USER, False),
    ("delivered_records", "delivered_records_user_isolation", _USER, False),
    ("pending_skip_trace_rows", "pending_skip_trace_rows_user_isolation", _USER, False),
    ("skip_trace_queues", "skip_trace_queues_user_isolation", _USER, False),
    ("password_history", "password_history_user_isolation", _USER, False),
    ("job_logs", "job_logs_via_job", _JOBLOGS, False),
    ("referral_events", "referral_events_user_isolation", _REF, False),
    ("user_record_views", "urv_user_only", _USER, True),  # 023 created this FOR ALL
]


def _recreate(table: str, policy: str, predicate: str, for_all: bool, *, with_escape: bool) -> None:
    op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    expr = f"({predicate}) OR {_SYS}" if with_escape else f"({predicate})"
    forall = "FOR ALL " if for_all else ""
    if with_escape:
        op.execute(
            f"CREATE POLICY {policy} ON {table} {forall}"
            f"USING ({expr}) WITH CHECK ({expr})"
        )
    else:
        # Restore the prior USING-only form (no WITH CHECK, no escape).
        op.execute(f"CREATE POLICY {policy} ON {table} {forall}USING ({expr})")


def upgrade() -> None:
    for table, policy, predicate, for_all in _SIMPLE:
        _recreate(table, policy, predicate, for_all, with_escape=True)


def downgrade() -> None:
    for table, policy, predicate, for_all in _SIMPLE:
        _recreate(table, policy, predicate, for_all, with_escape=False)
