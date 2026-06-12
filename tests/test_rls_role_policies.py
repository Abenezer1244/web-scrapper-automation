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
from src.utils.crypto import blind_index

# Needs provisioned RLS cutover roles + RLS_ENFORCE — excluded from the unit CI job.
pytestmark = pytest.mark.integration


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
    ae, be = f"rt_a_{a[:8]}@bl.test", f"rt_b_{b[:8]}@bl.test"
    conn.execute(
        text("""
            INSERT INTO users (id, email, email_hmac, password_hash, plan, records_used,
                records_limit, is_active, is_admin, referral_credit_cents)
            VALUES
            (:a, :ae, :ah, :pw, 'starter', 0, 50, true, false, 0),
            (:b, :be, :bh, :pw, 'starter', 0, 50, true, false, 0)
        """),
        {"a": a, "ae": ae, "ah": blind_index(ae), "b": b,
         "be": be, "bh": blind_index(be), "pw": pw},
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


# ═════════════════════════════════════════════════════════════════════════════
# H1 drift tables (2026-06-12): MFA, batches, audit_events, dialer_deliveries.
# Policies installed by scripts/apply_rls_cutover_policies.sql; grants by
# scripts/provision_rls_roles.sql. Denial cases run inside SAVEPOINTs so a
# permission error doesn't poison the outer (rolled-back) transaction.
# ═════════════════════════════════════════════════════════════════════════════

from sqlalchemy.exc import DBAPIError  # noqa: E402  (test-local import)


def _set_guc(conn, user_id: str) -> None:
    conn.execute(
        text("SELECT set_config('app.current_user_id', :u, true)"), {"u": user_id}
    )


def test_mfa_backup_codes_app_crud_is_tenant_scoped(cutover_ready: bool) -> None:
    """App holds the single allowlisted DELETE — but only on its OWN rows."""
    with sync_engine.begin() as conn:
        user_a, user_b, _ra, _rb = _seed_two_tenants(conn)
        code_a, code_b = str(uuid.uuid4()), str(uuid.uuid4())
        conn.execute(
            text("""
                INSERT INTO mfa_backup_codes (id, user_id, code_hash)
                VALUES (:ca, :a, repeat('a', 64)), (:cb, :b, repeat('b', 64))
            """),
            {"ca": code_a, "a": user_a, "cb": code_b, "b": user_b},
        )

        conn.execute(text("SET LOCAL ROLE bridgeleads_app"))
        _set_guc(conn, user_a)

        # SELECT: only A's code is visible.
        seen = conn.execute(
            text("SELECT id FROM mfa_backup_codes WHERE id IN (:ca, :cb)"),
            {"ca": code_a, "cb": code_b},
        ).fetchall()
        assert {str(r[0]) for r in seen} == {code_a}, "app leaked another tenant's MFA codes"

        # UPDATE/DELETE against B's row: policy filters it → 0 rows, no error.
        n = conn.execute(
            text("UPDATE mfa_backup_codes SET used_at = now() WHERE id = :cb"),
            {"cb": code_b},
        ).rowcount
        assert n == 0, "app UPDATEd another tenant's MFA code"
        n = conn.execute(
            text("DELETE FROM mfa_backup_codes WHERE id = :cb"), {"cb": code_b}
        ).rowcount
        assert n == 0, "app DELETEd another tenant's MFA code"

        # Own-row DELETE (the /auth/mfa/enable replace-set path) works.
        n = conn.execute(
            text("DELETE FROM mfa_backup_codes WHERE id = :ca"), {"ca": code_a}
        ).rowcount
        assert n == 1, "app could not DELETE its own MFA code (enable/disable breaks)"

        # INSERT for self passes WITH CHECK; INSERT for another tenant is denied.
        conn.execute(
            text("""
                INSERT INTO mfa_backup_codes (id, user_id, code_hash)
                VALUES (:i, :a, repeat('c', 64))
            """),
            {"i": str(uuid.uuid4()), "a": user_a},
        )
        with pytest.raises(DBAPIError):
            with conn.begin_nested():
                conn.execute(
                    text("""
                        INSERT INTO mfa_backup_codes (id, user_id, code_hash)
                        VALUES (:i, :b, repeat('d', 64))
                    """),
                    {"i": str(uuid.uuid4()), "b": user_b},
                )

        conn.execute(text("RESET ROLE"))
        conn.rollback()


def test_batches_app_select_insert_only(cutover_ready: bool) -> None:
    """scraper_batches/batch_runs: app INSERTs+SELECTs own-tenant; UPDATE denied."""
    with sync_engine.begin() as conn:
        user_a, user_b, _ra, _rb = _seed_two_tenants(conn)
        batch_b = str(uuid.uuid4())
        conn.execute(
            text("""
                INSERT INTO scraper_batches (id, user_id, state, fields, enrichment,
                    schedule, deliver, status)
                VALUES (:i, :b, 'WA', '[]'::json, '[]'::json, '{}'::json, '{}'::json,
                        'active')
            """),
            {"i": batch_b, "b": user_b},
        )

        conn.execute(text("SET LOCAL ROLE bridgeleads_app"))
        _set_guc(conn, user_a)

        # INSERT own batch + its durable pending run (the POST /batches path).
        batch_a, run_a = str(uuid.uuid4()), str(uuid.uuid4())
        conn.execute(
            text("""
                INSERT INTO scraper_batches (id, user_id, state, fields, enrichment,
                    schedule, deliver, status)
                VALUES (:i, :a, 'WA', '[]'::json, '[]'::json, '{}'::json, '{}'::json,
                        'active')
            """),
            {"i": batch_a, "a": user_a},
        )
        conn.execute(
            text("""
                INSERT INTO batch_runs (id, batch_id, user_id, status, child_job_ids,
                    excluded_no_date_count)
                VALUES (:r, :bt, :a, 'pending', '[]'::json, 0)
            """),
            {"r": run_a, "bt": batch_a, "a": user_a},
        )

        # SELECT: B's batch is invisible.
        seen = conn.execute(
            text("SELECT id FROM scraper_batches WHERE id IN (:x, :y)"),
            {"x": batch_a, "y": batch_b},
        ).fetchall()
        assert {str(r[0]) for r in seen} == {batch_a}, "app leaked another tenant's batch"

        # INSERT for another tenant fails WITH CHECK.
        with pytest.raises(DBAPIError):
            with conn.begin_nested():
                conn.execute(
                    text("""
                        INSERT INTO scraper_batches (id, user_id, state, fields,
                            enrichment, schedule, deliver, status)
                        VALUES (:i, :b, 'WA', '[]'::json, '[]'::json, '{}'::json,
                                '{}'::json, 'active')
                    """),
                    {"i": str(uuid.uuid4()), "b": user_b},
                )

        # Lifecycle UPDATE is worker-only: app holds no UPDATE grant at all.
        with pytest.raises(DBAPIError):
            with conn.begin_nested():
                conn.execute(
                    text("UPDATE batch_runs SET status = 'running' WHERE id = :r"),
                    {"r": run_a},
                )

        conn.execute(text("RESET ROLE"))
        conn.rollback()


def test_audit_events_app_insert_only_no_read(cutover_ready: bool) -> None:
    """audit_events: app INSERTs with NO GUC + NULL user_id; SELECT is denied."""
    with sync_engine.begin() as conn:
        conn.execute(text("SET LOCAL ROLE bridgeleads_app"))
        # Deliberately NO GUC — mirrors the audit background task session.
        conn.execute(
            text("""
                INSERT INTO audit_events (id, event, user_id, ip, path, detail)
                VALUES (:i, 'rls_test_event', NULL, '127.0.0.1', '/test', 'h1')
            """),
            {"i": str(uuid.uuid4())},
        )
        # No read path for the app role — not even its own insert.
        with pytest.raises(DBAPIError):
            with conn.begin_nested():
                conn.execute(text("SELECT id FROM audit_events LIMIT 1"))

        conn.execute(text("RESET ROLE"))
        conn.rollback()


def test_dialer_deliveries_app_update_is_tenant_scoped(cutover_ready: bool) -> None:
    """dialer_deliveries: app resets its OWN failed rows; INSERT denied."""
    with sync_engine.begin() as conn:
        user_a, user_b, ra, rb = _seed_two_tenants(conn)
        # Look up each tenant's seeded job/config (as owner, pre-SET ROLE).
        rows = conn.execute(
            text("""
                SELECT r.id, r.job_id, j.scraper_config_id, r.user_id
                FROM results r JOIN jobs j ON j.id = r.job_id
                WHERE r.id IN (:ra, :rb)
            """),
            {"ra": ra, "rb": rb},
        ).fetchall()
        by_user = {str(r[3]): r for r in rows}
        dd_a, dd_b = str(uuid.uuid4()), str(uuid.uuid4())
        for dd_id, (res_id, job_id, cfg_id, uid) in (
            (dd_a, by_user[user_a]),
            (dd_b, by_user[user_b]),
        ):
            conn.execute(
                text("""
                    INSERT INTO dialer_deliveries (id, job_id, result_id, user_id,
                        scraper_config_id, vendor_id, status, attempts, last_error)
                    VALUES (:i, :j, :r, :u, :c, 'phoneburner', 'failed', 1, 'boom')
                """),
                {"i": dd_id, "j": job_id, "r": res_id, "u": uid, "c": cfg_id},
            )

        conn.execute(text("SET LOCAL ROLE bridgeleads_app"))
        _set_guc(conn, user_a)

        # The replay-route UPDATE: own failed row flips, the other tenant's doesn't.
        n = conn.execute(
            text("""
                UPDATE dialer_deliveries SET status = 'pending', last_error = NULL
                WHERE id IN (:x, :y) AND status = 'failed'
            """),
            {"x": dd_a, "y": dd_b},
        ).rowcount
        assert n == 1, f"replay UPDATE touched {n} rows — must reset ONLY the caller's"

        # INSERT is worker-only.
        with pytest.raises(DBAPIError):
            with conn.begin_nested():
                conn.execute(
                    text("""
                        INSERT INTO dialer_deliveries (id, job_id, result_id, user_id,
                            scraper_config_id, vendor_id, status, attempts)
                        VALUES (:i, :j, :r, :u, :c, 'phoneburner', 'pending', 0)
                    """),
                    {
                        "i": str(uuid.uuid4()),
                        "j": by_user[user_a][1],
                        "r": by_user[user_a][0],
                        "u": user_a,
                        "c": by_user[user_a][2],
                    },
                )

        conn.execute(text("RESET ROLE"))
        conn.rollback()
