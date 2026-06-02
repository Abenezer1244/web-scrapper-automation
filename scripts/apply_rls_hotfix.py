"""Emergency hotfix: close the Supabase anon-API exposure of county_records.

Applies the live state that migrations 027 + 028 will make permanent, for the
case where you must stop the bleeding BEFORE the full branch deploys via alembic.

Idempotent and SAFE on the current BYPASSRLS role (verified: both DATABASE_URL
and DATABASE_URL_SYNC = postgres/bypassrls):
  * ENABLE RLS on county_records  -> default-deny for the anon/authenticated
    PostgREST roles (closes the homeowner-PII read hole); the app bypasses RLS
    so its reads/writes are unaffected.
  * shared-read SELECT policy      -> any authenticated app session (sets
    app.current_user_id) may read the shared catalog; anon stays denied.

Uses a DIRECT (5432) connection, not the pooled pgbouncer one, so the DDL runs
cleanly. Runs inside one transaction (Postgres DDL is transactional).

Usage:
    PYTHONPATH=. python scripts/apply_rls_hotfix.py
"""
from __future__ import annotations

from sqlalchemy import create_engine, text

from src.config import settings

_DDL = [
    "ALTER TABLE public.county_records ENABLE ROW LEVEL SECURITY",
    "DROP POLICY IF EXISTS county_records_shared_read ON county_records",
    (
        "CREATE POLICY county_records_shared_read ON county_records "
        "FOR SELECT USING ("
        "NULLIF(current_setting('app.current_user_id', true), '') IS NOT NULL)"
    ),
]


def _sync_direct_url() -> str:
    return settings.DATABASE_URL_SYNC.replace("+asyncpg", "").replace(":6543/", ":5432/")


def main() -> None:
    engine = create_engine(_sync_direct_url(), connect_args={"connect_timeout": 10})
    try:
        with engine.begin() as conn:
            for stmt in _DDL:
                conn.execute(text(stmt))
                print(f"OK: {stmt[:70]}")
        print("\nApplied. county_records: RLS enabled + shared-read policy.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
