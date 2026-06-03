"""Phase 2d RLS cutover: role-targeted policy enforcement (migration 030).

Proves the post-cutover policy model: under the NOBYPASSRLS cutover roles,
bridgeleads_app is tenant-isolated (own rows only, nothing without a GUC) while
bridgeleads_system reads cross-tenant. Exercised via SET LOCAL ROLE inside a
transaction, then rolled back so no fixtures persist.

These tests require the full cutover applied: both roles provisioned
(scripts/provision_rls_roles.sql) AND migration 030 run (which installs the
role-targeted policies). When that state is absent — CI/test DBs that kept the
legacy untargeted policies — the whole module SKIPS (test_rls_isolation.py
covers the legacy model there).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from src.api.auth import hash_password
from src.db.session import sync_engine


def _cutover_applied(conn) -> bool:
    """True only when both cutover roles AND a role-targeted policy exist."""
    roles = conn.execute(
        text("""
            SELECT COUNT(*) FROM pg_roles
            WHERE rolname IN ('bridgeleads_app', 'bridgeleads_system')
        """)
    ).scalar()
    policy = conn.execute(
        text("SELECT COUNT(*) FROM pg_policies WHERE policyname = 'results_app'")
    ).scalar()
    return roles == 2 and policy == 1


@pytest.fixture(scope="module")
def cutover_ready() -> bool:
    with sync_engine.begin() as conn:
        if not _cutover_applied(conn):
            pytest.skip("RLS cutover not applied (roles + migration 030) — "
                        "legacy model covered by test_rls_isolation.py")
        # Allow the connecting (owner) role to SET ROLE to both cutover roles.
        current = conn.execute(text("SELECT current_user")).scalar()
        for role in ("bridgeleads_app", "bridgeleads_system"):
            conn.execute(text(f'GRANT {role} TO "{current}"'))
    return True


def _seed_two_tenants(conn) -> tuple[str, str, str, str]:
    """Seed users A/B each with a config+job+result. Returns the result ids."""
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    pw = hash_password("testpassword123")
    conn.execute(
        text("""
            INSERT INTO users (id, email, password_hash, plan, records_used,
                records_limit, is_active, is_admin, referral_credit_cents)
            VALUES
            (:a, :ae, :pw, 'starter', 0, 50, true, false, 0),
            (:b, :be, :pw, 'starter', 0, 50, true, false, 0)
        """),
        {"a": a, "ae": f"rt_a_{a[:8]}@bl.test", "b": b,
         "be": f"rt_b_{b[:8]}@bl.test", "pw": pw},
    )
    ra, rb = None, None
    for uid, holder in ((a, "ra"), (b, "rb")):
        sc, job, res = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        conn.execute(
            text("""
                INSERT INTO scraper_configs (id, user_id, name, county, state,
                    record_type, fields, enrichment, schedule, deliver,
                    skip_trace_enabled, active)
                VALUES (:sc, :u, 'cfg', 'pierce', 'WA', 'probate',
                    '[]'::json, '[]'::json, '{}'::json, '{}'::json, false, true)
            """),
            {"sc": sc, "u": uid},
        )
        conn.execute(
            text("""
                INSERT INTO jobs (id, user_id, scraper_config_id, status, trigger,
                    page_current, page_total, record_count, retry_count)
                VALUES (:j, :u, :sc, 'done', 'manual', 0, 0, 1, 0)
            """),
            {"j": job, "u": uid, "sc": sc},
        )
        conn.execute(
            text("""
                INSERT INTO results (id, job_id, user_id, party_name, parcel_id,
                    skip_trace_status, is_duplicate)
                VALUES (:r, :j, :u, 'P', 'PARCEL', 'not_attempted', false)
            """),
            {"r": res, "j": job, "u": uid},
        )
        if holder == "ra":
            ra = res
        else:
            rb = res
    return a, b, ra, rb


def _visible_results(conn, ra: str, rb: str) -> set[str]:
    rows = conn.execute(
        text("SELECT id FROM results WHERE id IN (:ra, :rb)"),
        {"ra": ra, "rb": rb},
    ).fetchall()
    return {str(r[0]) for r in rows}


def test_app_role_is_tenant_isolated(cutover_ready: bool) -> None:
    """bridgeleads_app sees only its own rows, and nothing without a GUC."""
    with sync_engine.begin() as conn:
        user_a, user_b, ra, rb = _seed_two_tenants(conn)

        conn.execute(text("SET LOCAL ROLE bridgeleads_app"))

        # No GUC → tenant policy hides every row.
        assert _visible_results(conn, ra, rb) == set(), (
            "app role with no GUC must see zero tenant rows"
        )

        # GUC = A → only A's row.
        conn.execute(
            text("SELECT set_config('app.current_user_id', :u, true)"),
            {"u": user_a},
        )
        assert _visible_results(conn, ra, rb) == {ra}, "app role A leaked B's row"

        # GUC = B → only B's row.
        conn.execute(
            text("SELECT set_config('app.current_user_id', :u, true)"),
            {"u": user_b},
        )
        assert _visible_results(conn, ra, rb) == {rb}, "app role B leaked A's row"

        conn.execute(text("RESET ROLE"))
        conn.rollback()


def test_system_role_reads_cross_tenant(cutover_ready: bool) -> None:
    """bridgeleads_system sees every tenant's rows (no GUC)."""
    with sync_engine.begin() as conn:
        _user_a, _user_b, ra, rb = _seed_two_tenants(conn)

        conn.execute(text("SET LOCAL ROLE bridgeleads_system"))
        # FOR ALL USING(true) → both rows visible with no GUC set.
        assert _visible_results(conn, ra, rb) == {ra, rb}, (
            "system role must read across tenants"
        )

        conn.execute(text("RESET ROLE"))
        conn.rollback()
