"""Emergency hotfix: close any read exposure of county_records.

Applies the read side of migrations 027 + 119, for the case where you must stop
the bleeding BEFORE a branch deploys via alembic. Idempotent:
  * ENABLE RLS on county_records  -> default-deny for the anon/authenticated
    PostgREST roles (closes the homeowner-PII read hole).
  * drop both read policies       -> 028's county_records_shared_read and the
    cutover's county_records_app_read; nothing in the API reads this table.
  * REVOKE ALL from bridgeleads_app (when the role exists) -> the API role
    cannot read it even if a read policy came back.
The worker's county_records_system policy and grants (weekly purge) are left alone.

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
    "DROP POLICY IF EXISTS county_records_app_read ON county_records",
    (
        "DO $no_app_read$ BEGIN "
        "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_app') THEN "
        "REVOKE ALL ON public.county_records FROM bridgeleads_app; END IF; "
        "END $no_app_read$"
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
        print("\nApplied. county_records: RLS enabled, no read policy, no app grant.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
