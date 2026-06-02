"""Add WITH CHECK to user-scoped RLS write policies (REDTEAM HIGH T2).

The policies created in 001 and re-created in 018 used a ``USING``
clause only. In PostgreSQL, a policy with no ``WITH CHECK`` falls back
to the ``USING`` expression for reads but does NOT constrain the NEW
row on INSERT/UPDATE the way an explicit ``WITH CHECK`` does — and for
``INSERT`` there is no ``USING`` to fall back to at all, so a missing
``WITH CHECK`` means INSERT is governed only by the application code.

REDTEAM HIGH T2 (docs/security/REDTEAM-2026-06-01.md): with no
``WITH CHECK`` on the write policies, a tenant boundary on writes is
enforced *only* by the per-query ``user_id`` filter in application
code. If the runtime role also carries BYPASSRLS (see the fail-closed
check added in src/db/session.py), the policies are decorative. Adding
``WITH CHECK`` that mirrors the ``USING`` clause makes the DB reject any
INSERT/UPDATE whose ``user_id`` (or, for job_logs, owning job) does not
belong to the current ``app.current_user_id`` — closing the write side
of the tenant boundary at the database layer.

The clauses below reuse the crash-safe NULLIF pattern established in
018 (an unset ``app.current_user_id`` returns '' which cannot cast to
uuid; NULLIF turns '' into NULL so the predicate evaluates false and
hides/blocks the row instead of raising).

Revision ID: 025
Revises: 024
Create Date: 2026-06-01
"""

from alembic import op

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


# (table, policy, using/with-check predicate) — predicate is identical
# for USING and WITH CHECK so reads and writes share one tenant rule.
_USER_SCOPED_POLICIES = [
    (
        "scraper_configs",
        "scraper_configs_user_isolation",
        "user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
    ),
    (
        "jobs",
        "jobs_user_isolation",
        "user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
    ),
    (
        "results",
        "results_user_isolation",
        "user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
    ),
    (
        # job_logs has no user_id column; ownership flows through the
        # parent job. WITH CHECK forbids inserting a log row for a job
        # the current user does not own.
        "job_logs",
        "job_logs_via_job",
        "job_id IN (SELECT id FROM jobs WHERE user_id = "
        "NULLIF(current_setting('app.current_user_id', true), '')::uuid)",
    ),
]


def upgrade() -> None:
    # DROP + CREATE (rather than ALTER POLICY ... WITH CHECK) so this is
    # safe regardless of whether the live policy text matches 001 or 018.
    # IF EXISTS keeps it idempotent on partially-migrated databases.
    for table_name, policy_name, predicate in _USER_SCOPED_POLICIES:
        op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table_name}")
        op.execute(f"""
            CREATE POLICY {policy_name} ON {table_name}
            USING ({predicate})
            WITH CHECK ({predicate})
        """)


def downgrade() -> None:
    # Revert to USING-only (the 018 state) — no WITH CHECK.
    for table_name, policy_name, predicate in _USER_SCOPED_POLICIES:
        op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table_name}")
        op.execute(f"""
            CREATE POLICY {policy_name} ON {table_name}
            USING ({predicate})
        """)
