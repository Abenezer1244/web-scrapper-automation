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

import psycopg2

_POLICY_SQL = "scripts/apply_rls_cutover_policies.sql"

# Faithful mirror of provision_rls_roles.sql grant block (post-fix, incl. the
# H1 drift tables 2026-06-12 — keep BOTH files in lockstep, Codex finding #9).
_GRANTS = [
    "GRANT USAGE ON SCHEMA public TO bridgeleads_app",
    "GRANT SELECT, INSERT, UPDATE ON users, scraper_configs, jobs, user_record_views TO bridgeleads_app",
    "GRANT SELECT, INSERT ON county_connectors, password_history TO bridgeleads_app",
    "GRANT SELECT ON results, job_logs, county_records, referral_events, "
    "property_list_membership TO bridgeleads_app",
    # H1 drift tables — mfa_backup_codes carries the SINGLE allowlisted app DELETE
    "GRANT SELECT, INSERT, UPDATE, DELETE ON mfa_backup_codes TO bridgeleads_app",
    "GRANT SELECT, UPDATE ON mfa_break_glass_codes TO bridgeleads_app",
    "GRANT SELECT, INSERT ON scraper_batches, batch_runs TO bridgeleads_app",
    "GRANT INSERT ON audit_events TO bridgeleads_app",
    "GRANT SELECT, UPDATE ON dialer_deliveries TO bridgeleads_app",
    # pending_registrations (074): register INSERT + verify SELECT/DELETE (2nd
    # allowlisted app DELETE — verify drops the address's sibling staging rows).
    "GRANT SELECT, INSERT, DELETE ON pending_registrations TO bridgeleads_app",
    "REVOKE DELETE ON users, scraper_configs, jobs, user_record_views FROM bridgeleads_app",
    "REVOKE UPDATE, DELETE ON county_connectors, password_history FROM bridgeleads_app",
    "REVOKE INSERT, UPDATE, DELETE ON results, job_logs, county_records, referral_events, "
    "property_list_membership FROM bridgeleads_app",
    "REVOKE ALL ON delivered_records, pending_skip_trace_rows, skip_trace_queues, "
    "skip_trace_cache, skip_trace_meter_events FROM bridgeleads_app",
    "REVOKE INSERT, DELETE ON mfa_break_glass_codes FROM bridgeleads_app",
    "REVOKE UPDATE, DELETE ON scraper_batches, batch_runs FROM bridgeleads_app",
    "REVOKE SELECT, UPDATE, DELETE ON audit_events FROM bridgeleads_app",
    "REVOKE INSERT, DELETE ON dialer_deliveries FROM bridgeleads_app",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bridgeleads_app",
    # system role
    "GRANT USAGE ON SCHEMA public TO bridgeleads_system",
    "GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO bridgeleads_system",
    "GRANT DELETE ON county_records TO bridgeleads_system",
    "GRANT DELETE ON property_list_membership TO bridgeleads_system",
    # H1: operator MFA reset (scripts/reset_user_mfa.py) deletes both MFA tables
    "GRANT DELETE ON mfa_backup_codes, mfa_break_glass_codes TO bridgeleads_system",
    # pending_registrations (074): worker dispatch SELECT/UPDATE (ALL TABLES) + purge DELETE
    "GRANT DELETE ON pending_registrations TO bridgeleads_system",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bridgeleads_system",
]

_VERIFY_APP_GRANTS = """
    SELECT COUNT(*) FROM information_schema.role_table_grants
    WHERE grantee = 'bridgeleads_app'
      AND (
        (privilege_type = 'DELETE'
            AND table_name NOT IN ('mfa_backup_codes','pending_registrations'))
        OR (privilege_type IN ('INSERT','UPDATE')
            AND table_name IN ('results','job_logs','county_records','referral_events',
                               'property_list_membership'))
        OR (privilege_type = 'UPDATE'
            AND table_name IN ('county_connectors','password_history'))
        OR table_name IN ('delivered_records','pending_skip_trace_rows',
                          'skip_trace_queues','skip_trace_cache','skip_trace_meter_events')
        OR (privilege_type = 'INSERT'
            AND table_name IN ('mfa_break_glass_codes','dialer_deliveries'))
        OR (privilege_type = 'UPDATE'
            AND table_name IN ('scraper_batches','batch_runs','audit_events'))
        OR (privilege_type = 'SELECT' AND table_name = 'audit_events')
      )
"""


def _admin_dsn() -> str:
    with open(".env", encoding="utf-8") as f:
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
    with open(_POLICY_SQL, encoding="utf-8") as f:
        # Drop psql meta-command lines (start with backslash); keep SQL + DO + BEGIN/COMMIT.
        return "".join(line for line in f if not line.lstrip().startswith("\\"))


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
