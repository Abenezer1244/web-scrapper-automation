"""Row-level security integration test (C2 from full-SaaS review).

Proves that PostgreSQL RLS policies defined in migrations 001 and
018 actually isolate tenants when the DB role does NOT have
BYPASSRLS. The production Supabase role `postgres` currently has
`bypassrls=true`, so these policies are decorative in production
until that role is swapped — but this test forces a
non-bypassing role via `SET LOCAL ROLE` inside a transaction and
verifies that user A cannot read user B's data, and that queries
with no `app.current_user_id` set return zero rows.

The test is idempotent: it creates two users, writes data owned
by each, runs the isolation assertions, then rolls back so no
fixtures persist.
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


@pytest.fixture(autouse=True, scope="module")
def _legacy_model_only() -> None:
    """SKIP this module once the H1 cutover is applied.

    These tests assert the LEGACY untargeted ``*_user_isolation`` policies
    (migrations 001/018) via a generic non-bypass role. The cutover script
    (apply_rls_cutover_policies.sql) deliberately DROPs those policies and
    replaces them with role-targeted ``*_app``/``*_system`` ones — under that
    model a generic role matches no policy and sees nothing, so this module
    would false-fail (caught in the 2026-06-12 scratch rehearsal). The
    role-targeted model is covered by test_rls_role_policies.py; the two
    modules cover mutually exclusive DB states.
    """
    with sync_engine.begin() as conn:
        cutover = conn.execute(
            text("SELECT COUNT(*) FROM pg_policies WHERE policyname = 'results_app'")
        ).scalar()
    if cutover:
        pytest.skip(
            "H1 cutover applied (role-targeted policies) — this legacy-model "
            "module is superseded by test_rls_role_policies.py"
        )


def _create_non_bypass_role_if_missing(conn) -> str:
    """Ensure a test-only role exists that does NOT have BYPASSRLS.

    The role is created idempotently. We grant it SELECT/INSERT/
    UPDATE/DELETE on the public schema so queries issued under
    SET ROLE can touch the tables we test. The role persists
    across test runs — that is fine.
    """
    role_name = "bridgeleads_rls_test"
    existing = conn.execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = :name"),
        {"name": role_name},
    ).scalar()
    if not existing:
        conn.execute(text(f'CREATE ROLE {role_name} NOBYPASSRLS NOINHERIT'))
        conn.execute(text(f'GRANT USAGE ON SCHEMA public TO {role_name}'))
        conn.execute(text(
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES '
            f'IN SCHEMA public TO {role_name}'
        ))
        conn.execute(text(
            f'GRANT USAGE, SELECT ON ALL SEQUENCES '
            f'IN SCHEMA public TO {role_name}'
        ))
    # Ensure current_user (e.g. `postgres` on Supabase) can SET ROLE
    # to this test role. Without this GRANT, `SET LOCAL ROLE` raises
    # "permission denied to set role" even from a superuser. This
    # GRANT is idempotent.
    current_user = conn.execute(text("SELECT current_user")).scalar()
    conn.execute(text(f'GRANT {role_name} TO "{current_user}"'))
    return role_name


@pytest.fixture(scope="module")
def rls_test_role() -> str:
    with sync_engine.begin() as conn:
        role = _create_non_bypass_role_if_missing(conn)
    return role


def test_rls_blocks_cross_tenant_read_on_results(rls_test_role: str) -> None:
    """User A cannot read user B's Result rows when RLS is active."""
    user_a_id = str(uuid.uuid4())
    user_b_id = str(uuid.uuid4())
    user_a_email = f"rls_test_a_{user_a_id[:8]}@bl.test"
    user_b_email = f"rls_test_b_{user_b_id[:8]}@bl.test"

    # Seed data as superuser. The connection BEGINs a transaction
    # here; we will roll it back at the end via the context
    # manager so no rows persist.
    with sync_engine.begin() as conn:
        pw = hash_password("testpassword123")
        conn.execute(
            text("""
                INSERT INTO users (
                    id, email, email_hmac, password_hash, plan, records_used,
                    records_limit, is_active, is_admin, referral_credit_cents
                ) VALUES
                (:a, :ae, :ah, :pw, 'starter', 0, 50, true, false, 0),
                (:b, :be, :bh, :pw, 'starter', 0, 50, true, false, 0)
            """),
            {
                "a": user_a_id, "ae": user_a_email, "ah": blind_index(user_a_email),
                "b": user_b_id, "be": user_b_email, "bh": blind_index(user_b_email),
                "pw": pw,
            },
        )
        # Seed a scraper_config owned by each user so we can then
        # seed a job whose FK points at it.
        sc_a_id = str(uuid.uuid4())
        sc_b_id = str(uuid.uuid4())
        conn.execute(
            text("""
                INSERT INTO scraper_configs (
                    id, user_id, name, county, state, record_type,
                    fields, enrichment, schedule, deliver,
                    skip_trace_enabled, active
                ) VALUES
                (:sa, :a, 'A cfg', 'pierce', 'WA', 'probate',
                 '[]'::json, '[]'::json, '{}'::json, '{}'::json, false, true),
                (:sb, :b, 'B cfg', 'pierce', 'WA', 'probate',
                 '[]'::json, '[]'::json, '{}'::json, '{}'::json, false, true)
            """),
            {"sa": sc_a_id, "a": user_a_id, "sb": sc_b_id, "b": user_b_id},
        )
        job_a_id = str(uuid.uuid4())
        job_b_id = str(uuid.uuid4())
        conn.execute(
            text("""
                INSERT INTO jobs (
                    id, user_id, scraper_config_id, status, trigger,
                    page_current, page_total, record_count, retry_count
                ) VALUES
                (:ja, :a, :sa, 'done', 'manual', 0, 0, 1, 0),
                (:jb, :b, :sb, 'done', 'manual', 0, 0, 1, 0)
            """),
            {"ja": job_a_id, "a": user_a_id, "sa": sc_a_id,
             "jb": job_b_id, "b": user_b_id, "sb": sc_b_id},
        )
        result_a_id = str(uuid.uuid4())
        result_b_id = str(uuid.uuid4())
        conn.execute(
            text("""
                INSERT INTO results (
                    id, job_id, user_id, party_name, parcel_id,
                    skip_trace_status, is_duplicate
                ) VALUES
                (:ra, :ja, :a, 'A-PARTY', 'A-PARCEL',
                 'not_attempted', false),
                (:rb, :jb, :b, 'B-PARTY', 'B-PARCEL',
                 'not_attempted', false)
            """),
            {"ra": result_a_id, "ja": job_a_id, "a": user_a_id,
             "rb": result_b_id, "jb": job_b_id, "b": user_b_id},
        )

        # Now become the non-bypass role and exercise RLS
        conn.execute(text(f"SET LOCAL ROLE {rls_test_role}"))

        # 1. With no current_user_id set, both rows are invisible.
        hidden = conn.execute(
            text("SELECT COUNT(*) FROM results WHERE id IN (:ra, :rb)"),
            {"ra": result_a_id, "rb": result_b_id},
        ).scalar()
        assert hidden == 0, (
            f"RLS BROKEN: got {hidden} rows with no current_user_id set "
            "(expected 0 because the policy filters every row)"
        )

        # 2. Set current_user_id to A — only A's row is visible.
        conn.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": user_a_id},
        )
        only_a = conn.execute(
            text("SELECT id FROM results WHERE id IN (:ra, :rb)"),
            {"ra": result_a_id, "rb": result_b_id},
        ).fetchall()
        # results.id is UUID, comparison needs string coercion
        ids_visible = {str(r[0]) for r in only_a}
        assert ids_visible == {result_a_id}, (
            f"RLS BROKEN: as user A, expected only A's row visible, "
            f"got {ids_visible}"
        )

        # 3. Set current_user_id to B — only B's row is visible.
        conn.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": user_b_id},
        )
        only_b = conn.execute(
            text("SELECT id FROM results WHERE id IN (:ra, :rb)"),
            {"ra": result_a_id, "rb": result_b_id},
        ).fetchall()
        ids_visible = {str(r[0]) for r in only_b}
        assert ids_visible == {result_b_id}, (
            f"RLS BROKEN: as user B, expected only B's row visible, "
            f"got {ids_visible}"
        )

        # Reset role so the outer rollback runs as superuser
        conn.execute(text("RESET ROLE"))

        # Roll back everything we seeded. Raising here inside a
        # sync_engine.begin() block triggers the rollback; using
        # a plain exception would noise up the test output, so we
        # explicitly rollback via a savepoint pattern.
        conn.rollback()


def test_rls_blocks_cross_tenant_read_on_delivered_records(
    rls_test_role: str,
) -> None:
    """Migration 018's delivered_records policy isolates tenants."""
    user_a_id = str(uuid.uuid4())
    user_b_id = str(uuid.uuid4())
    user_a_email = f"rls_dr_a_{user_a_id[:8]}@bl.test"
    user_b_email = f"rls_dr_b_{user_b_id[:8]}@bl.test"

    with sync_engine.begin() as conn:
        pw = hash_password("testpassword123")
        conn.execute(
            text("""
                INSERT INTO users (
                    id, email, email_hmac, password_hash, plan, records_used,
                    records_limit, is_active, is_admin, referral_credit_cents
                ) VALUES
                (:a, :ae, :ah, :pw, 'starter', 0, 50, true, false, 0),
                (:b, :be, :bh, :pw, 'starter', 0, 50, true, false, 0)
            """),
            {
                "a": user_a_id, "ae": user_a_email, "ah": blind_index(user_a_email),
                "b": user_b_id, "be": user_b_email, "bh": blind_index(user_b_email),
                "pw": pw,
            },
        )
        dr_a_id = str(uuid.uuid4())
        dr_b_id = str(uuid.uuid4())
        conn.execute(
            text("""
                INSERT INTO delivered_records (
                    id, user_id, dedup_hash, parcel_id, property_address
                ) VALUES
                (:da, :a, 'HASH_A', 'P-A', '1 A ST'),
                (:db, :b, 'HASH_B', 'P-B', '1 B ST')
            """),
            {"da": dr_a_id, "a": user_a_id,
             "db": dr_b_id, "b": user_b_id},
        )

        conn.execute(text(f"SET LOCAL ROLE {rls_test_role}"))

        hidden = conn.execute(
            text(
                "SELECT COUNT(*) FROM delivered_records "
                "WHERE id IN (:a, :b)"
            ),
            {"a": dr_a_id, "b": dr_b_id},
        ).scalar()
        assert hidden == 0, (
            f"delivered_records RLS BROKEN: got {hidden} rows with no "
            "current_user_id (expected 0)"
        )

        conn.execute(
            text("SELECT set_config('app.current_user_id', :uid, true)"),
            {"uid": user_a_id},
        )
        only_a = conn.execute(
            text(
                "SELECT id FROM delivered_records "
                "WHERE id IN (:a, :b)"
            ),
            {"a": dr_a_id, "b": dr_b_id},
        ).fetchall()
        assert {str(r[0]) for r in only_a} == {dr_a_id}, (
            "delivered_records RLS BROKEN: A could see B's row"
        )

        conn.execute(text("RESET ROLE"))
        conn.rollback()
