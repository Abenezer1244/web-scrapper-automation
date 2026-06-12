"""H1: enable RLS on the post-cutover-design drift tables. (056)

scraper_batches + batch_runs (050/052) and audit_events (055) were created
WITHOUT `ENABLE ROW LEVEL SECURITY` — unlike every other tenant table (043/045
/041 pattern). That left them exposed to Supabase's PostgREST surface (anon/
authenticated see default-deny ONLY on RLS-enabled tables, migration 027) and
outside the H1 enforcement net.

This migration is deliberately role-INDEPENDENT (the 030 lesson: role-dependent
DDL must not live in Alembic — it would no-op in CI and never re-run at
cutover):
- scraper_batches / batch_runs: ENABLE RLS + untargeted per-tenant GUC USING
  policy (mirrors 043's mfa_backup_codes_user_isolation). Inert under today's
  BYPASSRLS runtime role; constrains any non-bypass role to its own rows.
- audit_events: ENABLE RLS with NO policy = default-deny. The role-targeted
  app INSERT-only + system FOR ALL policies land in
  scripts/apply_rls_cutover_policies.sql (operator script, run at cutover).
  Inert today (BYPASSRLS); locks PostgREST anon/authenticated out immediately.

The role-targeted _app/_system replacements for the user_isolation policies
are installed (and the untargeted ones dropped) by the cutover script.

Revision ID: 056
Revises: 055
Create Date: 2026-06-12
"""
from alembic import op

revision = "056"
down_revision = "055"
branch_labels = None
depends_on = None

_GUC_PREDICATE = (
    "user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid"
)


def upgrade() -> None:
    for table in ("scraper_batches", "batch_runs"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_user_isolation
            ON {table}
            USING ({_GUC_PREDICATE})
            """
        )
    # Default-deny: no policy on purpose (see docstring).
    op.execute("ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    # Deliberately does NOT disable RLS (Codex challenge P2): turning it back
    # off would reopen the PostgREST anon exposure this migration closes —
    # exactly the wrong thing during an emergency. The operational rollback for
    # the H1 cutover is repoint-URLs + RLS_ENFORCE=False (+ NO FORCE), never an
    # Alembic downgrade. This drops only the policies 056 added.
    for table in ("batch_runs", "scraper_batches"):
        op.execute(f"DROP POLICY IF EXISTS {table}_user_isolation ON {table}")
