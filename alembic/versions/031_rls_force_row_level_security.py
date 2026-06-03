"""Phase 4 RLS cutover: FORCE ROW LEVEL SECURITY (the LAST step).

By default a table's OWNER bypasses its own RLS policies. FORCE ROW LEVEL
SECURITY removes that exemption so even the owner is subject to the policies —
defence-in-depth in case the owner role is ever used for runtime traffic.

This is deliberately the FINAL migration of the cutover and is gated hard:

1. NO-OP unless BOTH cutover roles exist. In CI/test DBs (which connect as the
   owner/superuser and seed data directly), applying FORCE could block the
   owner's own seed INSERTs — so we skip entirely there.

2. OWNER-BYPASS GUARD (Codex). The SECURITY DEFINER helpers from migration 029
   (grant_referral_credit / activation_funnel) run as their owner. Under FORCE,
   the owner is subject to RLS, and those functions have no policy matching the
   owner role — so if the owner does NOT carry BYPASSRLS, FORCE would break
   them (the referral insert / cross-tenant reads would be denied). Supabase's
   `postgres` owner has BYPASSRLS, so this normally passes; if it does not, we
   RAISE rather than ship a broken cutover.

Operational ordering: run this ONLY after Phase 3 (roles provisioned + 029/030
applied + connections repointed) is verified healthy in staging, and flip
`RLS_ENFORCE=True` (env) alongside it. RLS_ENFORCE is an env flag, not a code
default — this migration does not touch it.

Revision ID: 031
Revises: 030
Create Date: 2026-06-02
"""

from alembic import op
from sqlalchemy import text

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


# Every RLS-enabled table that carries policies (excludes alembic_version, which
# stays default-deny and is only touched by the owner/migration role).
_FORCE_TABLES = [
    "scraper_configs", "jobs", "results", "job_logs", "delivered_records",
    "pending_skip_trace_rows", "skip_trace_queues", "password_history",
    "user_record_views", "referral_events", "users", "county_connectors",
    "county_records", "skip_trace_cache", "skip_trace_meter_events",
    "public_sample_cache",
]


def _both_roles_exist() -> bool:
    conn = op.get_bind()
    n = conn.execute(
        text("""
            SELECT COUNT(*) FROM pg_roles
            WHERE rolname IN ('bridgeleads_app', 'bridgeleads_system')
        """)
    ).scalar()
    return n == 2


def upgrade() -> None:
    if not _both_roles_exist():
        print("Phase 4 (031): cutover roles absent — skipping FORCE RLS "
              "(CI/test). No-op.")
        return

    # Owner-bypass guard: FORCE must not break the SECURITY DEFINER helpers.
    # Identify BOTH functions by exact signature (to_regprocedure returns NULL
    # rather than raising if a function is absent), then require every owner to
    # carry BYPASSRLS. (Codex review: bare proname + LIMIT 1 could match the
    # wrong schema/overload.)
    conn = op.get_bind()
    present = conn.execute(
        text("""
            SELECT to_regprocedure('public.grant_referral_credit(uuid)') AS grc,
                   to_regprocedure('public.activation_funnel(integer)')  AS af
        """)
    ).fetchone()
    if present.grc is None or present.af is None:
        raise RuntimeError(
            "Phase 4 (031): the SECURITY DEFINER helpers (grant_referral_credit / "
            "activation_funnel) are missing — migration 029 must be applied "
            "before FORCE RLS."
        )
    owner_bypasses = conn.execute(
        text("""
            SELECT bool_and(r.rolbypassrls)
            FROM pg_proc p
            JOIN pg_roles r ON r.oid = p.proowner
            WHERE p.oid IN (
                to_regprocedure('public.grant_referral_credit(uuid)'),
                to_regprocedure('public.activation_funnel(integer)')
            )
        """)
    ).scalar()
    if owner_bypasses is not True:
        raise RuntimeError(
            "Phase 4 (031): a SECURITY DEFINER function owner does NOT have "
            "BYPASSRLS. Applying FORCE ROW LEVEL SECURITY would block "
            "grant_referral_credit / activation_funnel (no policy matches the "
            "owner role). Make the function owner a BYPASSRLS role (or give the "
            "owner explicit policies) before forcing RLS."
        )

    for table in _FORCE_TABLES:
        op.execute(f"ALTER TABLE IF EXISTS public.{table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    # Not gated on role existence (Codex review): if FORCE was applied and the
    # roles were later dropped, downgrade must still lift FORCE. NO FORCE with
    # IF EXISTS is harmless on tables that were never forced.
    for table in _FORCE_TABLES:
        op.execute(f"ALTER TABLE IF EXISTS public.{table} NO FORCE ROW LEVEL SECURITY")
