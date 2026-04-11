"""Batch 2 C1: enable row-level security on Sprint 4/6/7 tables.

The initial migration (001) enabled RLS on scraper_configs, jobs,
results, and job_logs. Sprints 4, 6, and 7 added five more
user-scoped tables without RLS, breaking the "belt and suspenders"
promise in CLAUDE.md:

- delivered_records       (Sprint 6.4 — dedup per user)
- pending_skip_trace_rows (Sprint 4   — skip trace queue)
- skip_trace_queues       (Sprint 4   — Tracerfy batch log)
- password_history        (Sprint ?    — password-reuse prevention)
- referral_events         (Sprint 7.3 — referral audit log)

Today, any future endpoint that forgets the WHERE user_id filter
would silently leak rows across tenants with no DB backstop.

This migration enables RLS on all five tables and adds policies
that match the pattern used in 001 (`current_setting(
'app.current_user_id', true)::uuid`). The policies use `USING`
only (no `WITH CHECK`) because the worker writes to these tables
on behalf of users — the worker path can't reasonably set
app.current_user_id before every insert without plumbing the
user_id through every helper. Using `USING` means reads are
isolated across tenants, which is the security invariant we need.
Writes are still correct because the worker only inserts for the
current job's user_id, which is verified upstream.

The skip_trace_cache table is intentionally NOT made user-scoped.
It's a deduplication cache keyed on an address hash and is shared
across all users by design — see the docstring in models.py.

Revision ID: 018
Revises: 017
Create Date: 2026-04-11
"""

from alembic import op

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


# Every policy below uses NULLIF(..., '')::uuid instead of a naked
# ::uuid cast. When app.current_user_id is unset, Supabase returns
# an empty string '' from current_setting(..., true) rather than
# NULL, and '' cannot be cast to uuid — so a naked cast raises
# "invalid input syntax for type uuid" and kills the query instead
# of letting the policy evaluate to false. NULLIF converts '' to
# NULL before the cast; `user_id = NULL` evaluates to NULL (which
# is false in a policy context), so the policy correctly hides
# every row when no current_user_id is set.
_TABLES_WITH_USER_ID = [
    ("delivered_records", "delivered_records_user_isolation"),
    ("pending_skip_trace_rows", "pending_skip_trace_rows_user_isolation"),
    ("skip_trace_queues", "skip_trace_queues_user_isolation"),
    ("password_history", "password_history_user_isolation"),
]

# Existing policies from migration 001 that also used the unsafe
# naked-cast pattern. Re-creating them here with NULLIF makes every
# RLS policy in the codebase crash-safe.
_LEGACY_POLICIES = [
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
        "job_logs",
        "job_logs_via_job",
        "job_id IN (SELECT id FROM jobs WHERE user_id = "
        "NULLIF(current_setting('app.current_user_id', true), '')::uuid)",
    ),
]


def upgrade() -> None:
    # ── 1. New tables from Sprints 4/6/7 ────────────────────────────────
    for table_name, policy_name in _TABLES_WITH_USER_ID:
        op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {policy_name} ON {table_name}
            USING (
                user_id = NULLIF(
                    current_setting('app.current_user_id', true),
                    ''
                )::uuid
            )
        """)

    # referral_events has referrer_id AND referee_id — a user should
    # see events where they are EITHER the referrer (to audit their
    # own credit) or the referee (to see that their signup triggered
    # a bonus for someone else). The USING clause is a disjunction.
    op.execute("ALTER TABLE referral_events ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY referral_events_user_isolation ON referral_events
        USING (
            referrer_id = NULLIF(
                current_setting('app.current_user_id', true), ''
            )::uuid
            OR referee_id = NULLIF(
                current_setting('app.current_user_id', true), ''
            )::uuid
        )
    """)

    # ── 2. Patch legacy policies from migration 001 to also use NULLIF
    for table_name, policy_name, using_clause in _LEGACY_POLICIES:
        op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table_name}")
        op.execute(f"""
            CREATE POLICY {policy_name} ON {table_name}
            USING ({using_clause})
        """)


def downgrade() -> None:
    for table_name, policy_name in _TABLES_WITH_USER_ID:
        op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table_name}")
        op.execute(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY")

    op.execute(
        "DROP POLICY IF EXISTS referral_events_user_isolation ON referral_events"
    )
    op.execute("ALTER TABLE referral_events DISABLE ROW LEVEL SECURITY")

    # Restore the original (unsafe) legacy policies from migration 001
    legacy_original = [
        (
            "scraper_configs",
            "scraper_configs_user_isolation",
            "user_id = current_setting('app.current_user_id', true)::uuid",
        ),
        (
            "jobs",
            "jobs_user_isolation",
            "user_id = current_setting('app.current_user_id', true)::uuid",
        ),
        (
            "results",
            "results_user_isolation",
            "user_id = current_setting('app.current_user_id', true)::uuid",
        ),
        (
            "job_logs",
            "job_logs_via_job",
            "job_id IN (SELECT id FROM jobs WHERE user_id = "
            "current_setting('app.current_user_id', true)::uuid)",
        ),
    ]
    for table_name, policy_name, using_clause in legacy_original:
        op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table_name}")
        op.execute(f"""
            CREATE POLICY {policy_name} ON {table_name}
            USING ({using_clause})
        """)
