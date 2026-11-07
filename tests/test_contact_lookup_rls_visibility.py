"""Incident 2026-11-07: the worker could not see contact-lookup actions (migration 131).

Production carried only migration 101's untargeted tenant policy on the three ledger
tables, so bridgeleads_system (NOBYPASSRLS, no tenant GUC) saw zero rows: the worker's
dispatching -> running CAS and the reconciler's expiry both matched nothing, silently.

Each test first puts the tables back in that state (131's DOWNGRADE_SQL), proves the
failure, then applies 131's UPGRADE_SQL and proves the fix, as the real roles. Roles
are created when missing (CI has none) and everything is rolled back, as in
tests/test_contact_lookup_schema.py.
"""
import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from src.workers import contact_lookup_action as cla
from src.workers.scheduler import RLS_SYSTEM_GAPS_SQL
from tests.test_contact_lookup_schema import _action, _job_and_result

_TABLES = ("contact_lookup_actions", "contact_lookup_action_results",
           "contact_lookup_action_events")


def _mig131():
    path = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
            / "131_contact_lookup_system_policies.py")
    spec = importlib.util.spec_from_file_location("_mig131", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    return mig


async def _roles(db) -> None:
    for role in ("bridgeleads_app", "bridgeleads_system"):
        await db.execute(text(
            f"DO $r$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') "
            f"THEN CREATE ROLE {role} NOLOGIN NOBYPASSRLS; END IF; END $r$"))
        await db.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
    for t in _TABLES:
        await db.execute(text(f"GRANT SELECT, INSERT ON public.{t} TO bridgeleads_system"))
        await db.execute(text(f"GRANT SELECT, INSERT ON public.{t} TO bridgeleads_app"))
    await db.execute(text("GRANT UPDATE ON public.contact_lookup_actions TO bridgeleads_system"))
    await db.execute(text(
        "GRANT UPDATE (dispatched_at) ON public.contact_lookup_actions TO bridgeleads_app"))


async def _as(db, role: str, sql: str, params: dict) -> list:
    await db.execute(text(f"SET LOCAL ROLE {role}"))
    rows = (await db.execute(text(sql), params)).all()
    await db.execute(text("RESET ROLE"))
    return rows


async def test_the_worker_role_can_see_and_expire_an_action_only_after_131(db, business_user):
    mig = _mig131()
    job_id, _rid = await _job_and_result(db, business_user)
    aid = await _action(db, business_user, job_id)
    await _roles(db)
    see = "SELECT id FROM contact_lookup_actions WHERE id = CAST(:a AS uuid)"

    await db.execute(text(mig.DOWNGRADE_SQL))  # production before the fix
    assert await _as(db, "bridgeleads_system", see, {"a": aid}) == []  # the incident

    await db.execute(text(mig.UPGRADE_SQL))
    assert len(await _as(db, "bridgeleads_system", see, {"a": aid})) == 1
    # The reconciler's P1 move, and its event, as the worker role.
    moved = await _as(db, "bridgeleads_system",
                      "UPDATE contact_lookup_actions SET status = 'expired', "
                      "status_reason = 'deadline', status_changed_at = now() "
                      "WHERE id = CAST(:a AS uuid) AND status = 'dispatching' RETURNING id",
                      {"a": aid})
    assert len(moved) == 1
    event = await _as(db, "bridgeleads_system",
                      "INSERT INTO contact_lookup_action_events "
                      "(id, action_id, user_id, from_status, to_status, reason) "
                      "VALUES (:i, :a, :u, 'dispatching', 'expired', 'deadline') RETURNING id",
                      {"i": str(uuid.uuid4()), "a": aid, "u": business_user.id})
    assert len(event) == 1
    await db.rollback()


async def test_the_api_stamp_stays_tenant_scoped_and_dispatching_only(
    db, business_user, starter_user,
):
    mig = _mig131()
    job_id, _rid = await _job_and_result(db, business_user)
    aid = await _action(db, business_user, job_id)
    await _roles(db)
    await db.execute(text(mig.UPGRADE_SQL))
    stamp = ("UPDATE contact_lookup_actions SET dispatched_at = now() "
             "WHERE id = CAST(:a AS uuid) AND status = 'dispatching' "
             "AND dispatched_at IS NULL RETURNING id")

    await db.execute(text("SELECT set_config('app.current_user_id', :u, true)"),
                     {"u": starter_user.id})
    assert await _as(db, "bridgeleads_app", stamp, {"a": aid}) == []  # another tenant
    await db.execute(text("SELECT set_config('app.current_user_id', :u, true)"),
                     {"u": business_user.id})
    assert len(await _as(db, "bridgeleads_app", stamp, {"a": aid})) == 1

    # Once the worker has moved it on, the app's UPDATE policy no longer matches.
    await db.execute(text("SELECT set_config('app.current_user_id', '', true)"))  # the worker
    await db.execute(text("UPDATE contact_lookup_actions SET dispatched_at = NULL, "
                          "status = 'expired' WHERE id = CAST(:a AS uuid)"), {"a": aid})
    await db.execute(text("SELECT set_config('app.current_user_id', :u, true)"),
                     {"u": business_user.id})
    assert await _as(db, "bridgeleads_app",
                     "UPDATE contact_lookup_actions SET dispatched_at = now() "
                     "WHERE id = CAST(:a AS uuid) RETURNING id", {"a": aid}) == []
    await db.rollback()


async def test_upgrade_is_idempotent_and_downgrade_restores_only_the_tenant_policy(db):
    mig = _mig131()
    await _roles(db)
    await db.execute(text(mig.UPGRADE_SQL))
    await db.execute(text(mig.UPGRADE_SQL))  # a replayed boot must not fail
    policies = """SELECT tablename || '.' || policyname FROM pg_policies
                  WHERE tablename = ANY(CAST(:t AS text[])) ORDER BY 1"""
    up = set((await db.execute(text(policies), {"t": list(_TABLES)})).scalars())
    assert {f"{t}.{t}_user_isolation" for t in _TABLES}.isdisjoint(up)
    assert {f"{t}.{t}_system" for t in _TABLES[:2]} <= up

    await db.execute(text(mig.DOWNGRADE_SQL))
    down = set((await db.execute(text(policies), {"t": list(_TABLES)})).scalars())
    assert down == {f"{t}.{t}_user_isolation" for t in _TABLES}
    await db.rollback()


async def test_the_drift_audit_flags_a_table_the_worker_cannot_select(db):
    mig = _mig131()
    await _roles(db)
    gaps = "SELECT * FROM (" + RLS_SYSTEM_GAPS_SQL + ") g"

    await db.execute(text(mig.DOWNGRADE_SQL))  # the incident's catalog: flagged
    assert set(_TABLES) <= set((await db.execute(text(gaps))).scalars())
    await db.execute(text(mig.UPGRADE_SQL))
    assert set(_TABLES).isdisjoint((await db.execute(text(gaps))).scalars())

    # A policy that names the role but cannot admit a SELECT does not count.
    await db.execute(text("CREATE TABLE public.rls_gap_probe (id int)"))
    await db.execute(text("ALTER TABLE public.rls_gap_probe ENABLE ROW LEVEL SECURITY"))
    await db.execute(text("GRANT SELECT ON public.rls_gap_probe TO bridgeleads_system"))
    await db.execute(text("CREATE POLICY p_public ON public.rls_gap_probe USING (true)"))
    await db.execute(text("CREATE POLICY p_ins ON public.rls_gap_probe "
                          "FOR INSERT TO bridgeleads_system WITH CHECK (true)"))
    assert "rls_gap_probe" in set((await db.execute(text(gaps))).scalars())
    await db.execute(text("CREATE POLICY p_sel ON public.rls_gap_probe "
                          "FOR SELECT TO bridgeleads_system USING (true)"))
    assert "rls_gap_probe" not in set((await db.execute(text(gaps))).scalars())
    await db.rollback()


async def test_a_half_provisioned_cluster_fails_the_migration_instead_of_skipping(db):
    mig = _mig131()
    present = set((await db.execute(text(
        "SELECT rolname FROM pg_roles "
        "WHERE rolname IN ('bridgeleads_app', 'bridgeleads_system')"))).scalars())
    if present:
        pytest.skip("this cluster already provisions a runtime role")
    await db.execute(text("CREATE ROLE bridgeleads_system NOLOGIN NOBYPASSRLS"))
    with pytest.raises(Exception) as exc:
        await db.execute(text(mig.UPGRADE_SQL))
    assert "only one of bridgeleads_app/bridgeleads_system" in str(exc.value)
    await db.rollback()


async def test_an_action_the_worker_cannot_see_is_reported_not_swallowed(db):
    assert cla.run_action(str(uuid.uuid4())) == {"outcome": "action_not_visible"}
