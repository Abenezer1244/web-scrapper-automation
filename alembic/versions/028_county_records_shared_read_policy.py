"""Shared-read SELECT policy on county_records (forward-compat for non-bypass RLS).

Migration 027 ENABLEs RLS on county_records (closing the Supabase anon-API
exposure of scraped homeowner PII). With NO policy, RLS is default-deny for
every role subject to it. That is correct TODAY because the app connects with a
BYPASSRLS role (verified live: both DATABASE_URL and DATABASE_URL_SYNC =
`postgres`, bypassrls=true) — the app bypasses RLS so its reads work, and the
anon/authenticated PostgREST roles are denied.

But county_records is a SHARED catalog (no user_id) that the app reads via
get_rls_db in get_cached_records (src/api/routes/scrapers.py:~501-540), which
sets app.current_user_id. When the deferred HIGH-2 cutover downgrades the app to
a NON-bypass role, default-deny would block that read and break /scrapers cached
records (Codex flagged this).

This migration adds a SELECT policy that allows the shared read for ANY
authenticated app session (one that has set app.current_user_id) while still
denying the Supabase anon role — anon never sets that GUC, so the policy
evaluates false for it. No per-user filter: county_records is shared, every
authenticated user sees the whole catalog (the documented freemium model).

Inert under the current BYPASSRLS role (the app bypasses RLS for reads anyway),
so this is a safe no-op now and ready for the cutover. county_records WRITES
(system_sync_session, no GUC) under a future non-bypass role still need the
system-role handling that is part of the broader HIGH-2 cutover — out of scope
here; the 023 write-trigger continues to block user-scoped writes meanwhile.

NULLIF(..., '')::-style guard matches the crash-safe pattern from migration 018
(Supabase returns '' not NULL from current_setting(..., true) when unset).
"""
from alembic import op

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None

_POLICY = "county_records_shared_read"


def upgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON county_records")
    op.execute(
        f"""
        CREATE POLICY {_POLICY} ON county_records
            FOR SELECT
            USING (
                NULLIF(current_setting('app.current_user_id', true), '') IS NOT NULL
            )
        """
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON county_records")
