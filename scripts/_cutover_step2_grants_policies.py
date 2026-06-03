"""RLS cutover — STEP 2 (prod): table grants/revokes + role-targeted policies.

Inert under the current BYPASSRLS `postgres` role (grants/policies don't affect
a bypassing role, and the new roles serve no traffic until the repoint). Runs as
postgres via the SESSION pooler (:5432). Idempotent.

Part A: table GRANT/REVOKE — mirrors scripts/provision_rls_roles.sql (the fixed,
        Codex-reviewed grant block) exactly: app least-privilege + explicit
        REVOKEs + a hard-fail verify. (Role creation already done in step 1.)
Part B: executes scripts/apply_rls_cutover_policies.sql (role-targeted policies +
        029 binding backfill), stripping psql meta-commands so psycopg2 can run it.

Run:  PYTHONPATH=. python scripts/_cutover_step2_grants_policies.py
"""

from __future__ import annotations

import io
import sys

import psycopg2

_POLICY_SQL = "scripts/apply_rls_cutover_policies.sql"

# Faithful mirror of provision_rls_roles.sql grant block (post-fix).
_GRANTS = [
    "GRANT USAGE ON SCHEMA public TO bridgeleads_app",
    "GRANT SELECT, INSERT, UPDATE ON users, scraper_configs, jobs, user_record_views TO bridgeleads_app",
    "GRANT SELECT, INSERT ON county_connectors, password_history TO bridgeleads_app",
    "GRANT SELECT ON results, job_logs, county_records, referral_events TO bridgeleads_app",
    "REVOKE DELETE ON users, scraper_configs, jobs, user_record_views FROM bridgeleads_app",
    "REVOKE UPDATE, DELETE ON county_connectors, password_history FROM bridgeleads_app",
    "REVOKE INSERT, UPDATE, DELETE ON results, job_logs, county_records, referral_events FROM bridgeleads_app",
    "REVOKE ALL ON delivered_records, pending_skip_trace_rows, skip_trace_queues, "
    "skip_trace_cache, skip_trace_meter_events FROM bridgeleads_app",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bridgeleads_app",
    # system role
    "GRANT USAGE ON SCHEMA public TO bridgeleads_system",
    "GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO bridgeleads_system",
    "GRANT DELETE ON county_records TO bridgeleads_system",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bridgeleads_system",
]

_VERIFY_APP_GRANTS = """
    SELECT COUNT(*) FROM information_schema.role_table_grants
    WHERE grantee = 'bridgeleads_app'
      AND (
        privilege_type = 'DELETE'
        OR (privilege_type IN ('INSERT','UPDATE')
            AND table_name IN ('results','job_logs','county_records','referral_events'))
        OR (privilege_type = 'UPDATE'
            AND table_name IN ('county_connectors','password_history'))
        OR table_name IN ('delivered_records','pending_skip_trace_rows',
                          'skip_trace_queues','skip_trace_cache','skip_trace_meter_events')
      )
"""


def _admin_dsn() -> str:
    with io.open(".env", "r", encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith("DATABASE_URL_SYNC="):
                dsn = line.strip().split("=", 1)[1].replace("postgresql+psycopg2://", "postgresql://")
                break
        else:
            raise SystemExit("DATABASE_URL_SYNC not found")
    if ":5432/" not in dsn or "pooler.supabase.com" not in dsn:
        raise SystemExit("refusing: admin DSN must be the :5432 Supabase session pooler")
    return dsn


def _policy_sql_without_psql_meta() -> str:
    with io.open(_POLICY_SQL, "r", encoding="utf-8") as f:
        # Drop psql meta-command lines (start with backslash); keep SQL + DO + BEGIN/COMMIT.
        return "".join(l for l in f if not l.lstrip().startswith("\\"))


def main() -> None:
    dsn = _admin_dsn()
    conn = psycopg2.connect(dsn, connect_timeout=20)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # Operational safety (Codex review): policy DROP/CREATE takes DDL locks
            # on LIVE tables. Bound the wait so we can never build a prod blocking
            # queue — fail fast instead.
            cur.execute("SET lock_timeout = '5s'")
            cur.execute("SET statement_timeout = '120s'")
            print("== Part A: table grants/revokes ==")
            for stmt in _GRANTS:
                cur.execute(stmt)
            cur.execute(_VERIFY_APP_GRANTS)
            bad = cur.fetchone()[0]
            if bad:
                raise SystemExit(f"app role holds {bad} disallowed privilege(s) — convergence failed")
            print("  grants applied; app least-privilege verified (0 disallowed)")

            print("== Part B: role-targeted policies (apply_rls_cutover_policies.sql) ==")
            cur.execute(_policy_sql_without_psql_meta())
            # The script ends with a verification SELECT; surface its rows.
            try:
                rows = cur.fetchall()
                print(f"  policies applied; {len(rows)} role-targeted policies present:")
                for r in rows:
                    print(f"    {r[1]}.{r[2]} roles={r[3]} cmd={r[4]}")
            except psycopg2.ProgrammingError:
                print("  policies applied (no result set to show)")
    finally:
        conn.close()
    print("\n>>> STEP 2 done. Inert under BYPASSRLS. Next: rehearse as the new roles.")


if __name__ == "__main__":
    main()
