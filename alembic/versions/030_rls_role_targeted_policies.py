"""Phase 2c RLS cutover: role-targeted policies for app + system roles.

Replaces the UNTARGETED tenant policies (001/018/023/025/028 — they apply to
every role, PUBLIC included) with policies scoped to the two cutover roles:

  * bridgeleads_app    — FastAPI request traffic. Per-tenant isolation via the
    app.current_user_id GUC, broad access only where auth must query before a
    GUC exists (users) or for shared catalogs (county_connectors/records).
  * bridgeleads_system — Celery workers/scheduler. FOR ALL USING(true) on every
    table it touches (trusted cross-tenant infra). Permissive policies OR, so
    the system role is unrestricted while the app role stays tenant-scoped.

The Supabase anon/authenticated roles get NO policy on any of these tables, so
RLS default-denies them — the 027 PII lockout is preserved (and tightened: the
old untargeted policies kept those roles in the policy surface).

referral_events is SELECT-only for the app role: Phase 2b moved the WRITE into
the SECURITY DEFINER public.grant_referral_credit() (runs as the owner, bypasses
RLS), so the app role never inserts referral rows directly.

ROLE-EXISTENCE CONTRACT (Codex review): this migration is a no-op when NEITHER
cutover role exists (CI/test DBs that never ran scripts/provision_rls_roles.sql
— they keep the untargeted policies that tests/test_rls_isolation.py relies on).
When BOTH exist it performs the swap. When EXACTLY ONE exists it RAISES — a
half-provisioned cutover must not silently install the wrong policy model.

Inert under today's BYPASSRLS role (policies are skipped for a bypassing role).
RLS becomes load-bearing only after Phase 3 repoints to the non-bypass roles and
Phase 4 flips RLS_ENFORCE. FORCE ROW LEVEL SECURITY stays in Phase 4 — and Phase
4 must verify the SECURITY DEFINER function owner keeps BYPASSRLS (or the
definer helpers break under FORCE).

Revision ID: 030
Revises: 029
Create Date: 2026-06-02
"""

from alembic import op
from sqlalchemy import text

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


# app.current_user_id GUC → uuid, crash-safe (unset returns '' → NULL → false).
_GUC = "NULLIF(current_setting('app.current_user_id', true), '')::uuid"

# (table, existing untargeted policy name, app tenant predicate)
_TENANT = [
    ("scraper_configs", "scraper_configs_user_isolation", f"user_id = {_GUC}"),
    ("jobs", "jobs_user_isolation", f"user_id = {_GUC}"),
    ("results", "results_user_isolation", f"user_id = {_GUC}"),
    ("delivered_records", "delivered_records_user_isolation", f"user_id = {_GUC}"),
    ("pending_skip_trace_rows", "pending_skip_trace_rows_user_isolation", f"user_id = {_GUC}"),
    ("skip_trace_queues", "skip_trace_queues_user_isolation", f"user_id = {_GUC}"),
    ("password_history", "password_history_user_isolation", f"user_id = {_GUC}"),
    ("user_record_views", "urv_user_only", f"user_id = {_GUC}"),
    (
        "job_logs",
        "job_logs_via_job",
        f"job_id IN (SELECT id FROM jobs WHERE user_id = {_GUC})",
    ),
]

# Worker-only tables (027 default-deny): system FOR ALL, NO app policy.
_SYSTEM_ONLY = ["skip_trace_cache", "skip_trace_meter_events"]


def _roles_present() -> tuple[bool, bool]:
    conn = op.get_bind()
    has_app = conn.execute(
        text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_app')")
    ).scalar()
    has_sys = conn.execute(
        text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system')")
    ).scalar()
    return bool(has_app), bool(has_sys)


def upgrade() -> None:
    has_app, has_sys = _roles_present()
    if not has_app and not has_sys:
        print("Phase 2c (030): cutover roles absent — leaving untargeted "
              "policies in place (CI/test). No-op.")
        return
    if has_app != has_sys:
        raise RuntimeError(
            "Phase 2c (030): exactly one of bridgeleads_app / bridgeleads_system "
            "exists. Provision BOTH (scripts/provision_rls_roles.sql) before "
            "running this migration — a half-provisioned cutover would install "
            "the wrong policy model."
        )

    # ── Backfill 029 role bindings (Codex review) ────────────────────────────
    # 029's GRANT EXECUTE + public_sample_cache policies were conditional on the
    # roles existing AT 029-time. If the roles were provisioned AFTER 029 ran,
    # those bindings were skipped and never applied. 030 only runs when both
    # roles exist, so backfill them here (idempotent). Without this, /referral
    # (grant_referral_credit) and /scrapers/sample would fail under the cutover.
    op.execute("GRANT EXECUTE ON FUNCTION public.grant_referral_credit(uuid) TO bridgeleads_app")
    op.execute("GRANT EXECUTE ON FUNCTION public.activation_funnel(integer) TO bridgeleads_app")
    op.execute("GRANT SELECT ON public.public_sample_cache TO bridgeleads_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON public.public_sample_cache TO bridgeleads_system")
    op.execute("DROP POLICY IF EXISTS public_sample_cache_app_read ON public.public_sample_cache")
    op.execute("""
        CREATE POLICY public_sample_cache_app_read ON public.public_sample_cache
        FOR SELECT TO bridgeleads_app USING (true)
    """)
    op.execute("DROP POLICY IF EXISTS public_sample_cache_system_all ON public.public_sample_cache")
    op.execute("""
        CREATE POLICY public_sample_cache_system_all ON public.public_sample_cache
        FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true)
    """)

    # ── Tenant tables: drop untargeted, add app-tenant + system-all ──────────
    for table, old_policy, pred in _TENANT:
        op.execute(f"DROP POLICY IF EXISTS {old_policy} ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_app ON {table}")
        op.execute(f"""
            CREATE POLICY {table}_app ON {table}
            TO bridgeleads_app
            USING ({pred}) WITH CHECK ({pred})
        """)
        op.execute(f"DROP POLICY IF EXISTS {table}_system ON {table}")
        op.execute(f"""
            CREATE POLICY {table}_system ON {table}
            FOR ALL TO bridgeleads_system
            USING (true) WITH CHECK (true)
        """)

    # ── referral_events: app SELECT-only (writes via SECURITY DEFINER fn) ────
    op.execute("DROP POLICY IF EXISTS referral_events_user_isolation ON referral_events")
    op.execute("DROP POLICY IF EXISTS referral_events_app ON referral_events")
    op.execute(f"""
        CREATE POLICY referral_events_app ON referral_events
        FOR SELECT TO bridgeleads_app
        USING (referrer_id = {_GUC} OR referee_id = {_GUC})
    """)
    op.execute("DROP POLICY IF EXISTS referral_events_system ON referral_events")
    op.execute("""
        CREATE POLICY referral_events_system ON referral_events
        FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true)
    """)

    # ── users: broad app (auth queries users pre-GUC) + system all ───────────
    # Accepted floor (Codex): RLS cannot express "find the one row matching a
    # submitted email" without app-supplied identity. anon/authenticated stay
    # denied (no policy for them); the app-layer WHERE filters remain the
    # tenant boundary on this table.
    op.execute("DROP POLICY IF EXISTS users_app ON users")
    op.execute("""
        CREATE POLICY users_app ON users
        FOR ALL TO bridgeleads_app USING (true) WITH CHECK (true)
    """)
    op.execute("DROP POLICY IF EXISTS users_system ON users")
    op.execute("""
        CREATE POLICY users_system ON users
        FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true)
    """)

    # ── county_connectors: app SELECT + INSERT (POST /connectors), system all ─
    op.execute("DROP POLICY IF EXISTS county_connectors_app_read ON county_connectors")
    op.execute("""
        CREATE POLICY county_connectors_app_read ON county_connectors
        FOR SELECT TO bridgeleads_app USING (true)
    """)
    op.execute("DROP POLICY IF EXISTS county_connectors_app_insert ON county_connectors")
    op.execute("""
        CREATE POLICY county_connectors_app_insert ON county_connectors
        FOR INSERT TO bridgeleads_app WITH CHECK (true)
    """)
    op.execute("DROP POLICY IF EXISTS county_connectors_system ON county_connectors")
    op.execute("""
        CREATE POLICY county_connectors_system ON county_connectors
        FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true)
    """)

    # ── county_records: app shared-read (scoped) + system all (trigger guards) ─
    op.execute("DROP POLICY IF EXISTS county_records_shared_read ON county_records")
    op.execute("DROP POLICY IF EXISTS county_records_app_read ON county_records")
    op.execute("""
        CREATE POLICY county_records_app_read ON county_records
        FOR SELECT TO bridgeleads_app
        USING (NULLIF(current_setting('app.current_user_id', true), '') IS NOT NULL)
    """)
    op.execute("DROP POLICY IF EXISTS county_records_system ON county_records")
    op.execute("""
        CREATE POLICY county_records_system ON county_records
        FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true)
    """)

    # ── Worker-only tables: system FOR ALL, no app policy ────────────────────
    for table in _SYSTEM_ONLY:
        op.execute(f"DROP POLICY IF EXISTS {table}_system ON {table}")
        op.execute(f"""
            CREATE POLICY {table}_system ON {table}
            FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true)
        """)


def downgrade() -> None:
    has_app, has_sys = _roles_present()
    if not has_app and not has_sys:
        return  # nothing was applied on upgrade

    # Drop the role-targeted policies and restore the untargeted pre-2c state:
    # 025's USING+WITH CHECK on the four legacy tenant tables, USING-only on the
    # 018/023 tables, and the 028 county_records shared read.
    # 025 added WITH CHECK to these four; 018/023 tables were USING-only.
    _with_check = {"scraper_configs", "jobs", "results", "job_logs"}
    for table, old_policy, pred in _TENANT:
        op.execute(f"DROP POLICY IF EXISTS {table}_app ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_system ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {old_policy} ON {table}")
        if table in _with_check:
            op.execute(
                f"CREATE POLICY {old_policy} ON {table} "
                f"USING ({pred}) WITH CHECK ({pred})"
            )
        else:
            op.execute(f"CREATE POLICY {old_policy} ON {table} USING ({pred})")

    op.execute("DROP POLICY IF EXISTS referral_events_app ON referral_events")
    op.execute("DROP POLICY IF EXISTS referral_events_system ON referral_events")
    op.execute("DROP POLICY IF EXISTS referral_events_user_isolation ON referral_events")
    op.execute(f"""
        CREATE POLICY referral_events_user_isolation ON referral_events
        USING (referrer_id = {_GUC} OR referee_id = {_GUC})
    """)

    op.execute("DROP POLICY IF EXISTS users_app ON users")
    op.execute("DROP POLICY IF EXISTS users_system ON users")

    op.execute("DROP POLICY IF EXISTS county_connectors_app_read ON county_connectors")
    op.execute("DROP POLICY IF EXISTS county_connectors_app_insert ON county_connectors")
    op.execute("DROP POLICY IF EXISTS county_connectors_system ON county_connectors")

    op.execute("DROP POLICY IF EXISTS county_records_app_read ON county_records")
    op.execute("DROP POLICY IF EXISTS county_records_system ON county_records")
    op.execute("DROP POLICY IF EXISTS county_records_shared_read ON county_records")
    op.execute("""
        CREATE POLICY county_records_shared_read ON county_records
        FOR SELECT
        USING (NULLIF(current_setting('app.current_user_id', true), '') IS NOT NULL)
    """)

    for table in _SYSTEM_ONLY:
        op.execute(f"DROP POLICY IF EXISTS {table}_system ON {table}")
