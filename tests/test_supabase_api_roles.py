"""Finding 2026-11-07: the Supabase Data API roles reached public (migration 134).

Production's default privileges gave anon/authenticated ALL on every public table and
EXECUTE on every function, including SECURITY DEFINER privacy_rule_rows(), and the Data
API serves schema public to the publishable key. Each test recreates that state with the
real role names (created when missing; CI has none), applies 134's UPGRADE_SQL and checks
the catalog with API_ROLE_EXPOSURE_SQL, the query the hourly audit alerts on. Everything,
roles included, is rolled back.
"""
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text

from src.workers.scheduler import API_ROLE_EXPOSURE_SQL

_API_ROLES = ("anon", "authenticated", "service_role")


def _mig134():
    path = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
            / "134_revoke_supabase_api_roles.py")
    spec = importlib.util.spec_from_file_location("_mig134", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    return mig


async def _production_grants(db) -> None:
    """The state found in production: the migration owner's default privileges gave the
    API roles everything it owns (objects owned by bridgeleads_purge carried none)."""
    for role in _API_ROLES:
        await db.execute(text(
            f"DO $r$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') "
            f"THEN CREATE ROLE {role} NOLOGIN; END IF; END $r$"))
    await db.execute(text("GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role"))
    await db.execute(text("""
        DO $g$ DECLARE o record; BEGIN
          FOR o IN SELECT c.oid::regclass AS obj FROM pg_class c
                   JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
                   WHERE c.relkind IN ('r', 'v', 'm', 'p') AND c.relowner = current_user::regrole
          LOOP EXECUTE format('GRANT ALL ON TABLE %s TO anon, authenticated', o.obj); END LOOP;
          FOR o IN SELECT p.oid::regprocedure AS fn FROM pg_proc p
                   JOIN pg_namespace n ON n.oid = p.pronamespace AND n.nspname = 'public'
                   WHERE p.proowner = current_user::regrole AND p.prokind = 'f'
          LOOP EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO anon, authenticated, service_role',
                              o.fn); END LOOP;
        END $g$"""))
    await db.execute(text(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated"))


async def _exposed(db) -> set[str]:
    return set((await db.execute(text(API_ROLE_EXPOSURE_SQL))).scalars())


async def _has_privacy_reader(db) -> bool:
    return (await db.execute(text(
        "SELECT to_regprocedure('public.privacy_rule_rows()') IS NOT NULL"))).scalar()


async def test_the_api_roles_reach_nothing_after_134(db):
    await _production_grants(db)
    before = await _exposed(db)
    assert "anon:results" in before and "authenticated:users" in before  # the finding
    if await _has_privacy_reader(db):
        assert {"anon:privacy_rule_rows()", "service_role:privacy_rule_rows()"} <= before

    await db.execute(text(_mig134().UPGRADE_SQL))
    assert await _exposed(db) == set()
    await db.rollback()


async def test_the_worker_keeps_the_privacy_reader(db):
    if not await _has_privacy_reader(db):
        pytest.skip("migration 129 did not create privacy_rule_rows() on this cluster")
    await db.execute(text(
        "DO $r$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') "
        "THEN CREATE ROLE bridgeleads_system NOLOGIN NOBYPASSRLS; END IF; END $r$"))
    await db.execute(text(
        "GRANT EXECUTE ON FUNCTION public.privacy_rule_rows() TO bridgeleads_system"))
    await _production_grants(db)
    await db.execute(text(_mig134().UPGRADE_SQL))
    assert (await db.execute(text(
        "SELECT has_function_privilege('bridgeleads_system', "
        "'public.privacy_rule_rows()', 'EXECUTE')"))).scalar() is True
    await db.rollback()


async def test_a_table_created_after_134_is_not_granted_to_the_api_roles(db):
    await _production_grants(db)
    mig = _mig134()
    await db.execute(text(mig.UPGRADE_SQL))
    await db.execute(text(mig.UPGRADE_SQL))  # a replayed boot must not fail
    await db.execute(text("CREATE TABLE public.api_role_probe (id int)"))
    assert not {"anon:api_role_probe", "authenticated:api_role_probe"} & await _exposed(db)
    await db.rollback()


async def test_a_definer_function_the_api_can_still_call_fails_the_migration(db):
    """Revoking from anon does not remove PUBLIC's EXECUTE, Postgres's default: the
    postcondition must refuse rather than record 134 with a callable definer function."""
    await _production_grants(db)
    await db.execute(text(
        "CREATE FUNCTION public.api_role_probe_fn() RETURNS int "
        "LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'"))
    with pytest.raises(Exception) as exc:
        await db.execute(text(_mig134().UPGRADE_SQL))
    assert "can still call a SECURITY DEFINER function" in str(exc.value)
    await db.rollback()
