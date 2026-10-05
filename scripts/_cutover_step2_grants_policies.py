"""RLS cutover — STEP 2 (prod): table grants/revokes + role-targeted policies.

Inert under the current BYPASSRLS `postgres` role (grants/policies don't affect
a bypassing role, and the new roles serve no traffic until the repoint). Runs as
postgres via the SESSION pooler (:5432). Idempotent.

Part A: table GRANT/REVOKE — mirrors scripts/provision_rls_roles.sql (the fixed,
        Codex-reviewed grant block): app least-privilege + explicit REVOKEs + TWO
        hard-fail verifies — a negative one (app holds nothing extra) and a
        positive one (system still holds every DELETE the worker needs). The
        second exists because this file silently drifted from
        provision_rls_roles.sql by one line and prod lost DELETE on
        delivered_records; "mirrors exactly" is now enforced, not asserted.
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
    # audit_events (111): the app READS its own rows (policy-bounded); never writes back.
    "GRANT SELECT ON audit_events TO bridgeleads_app",
    "REVOKE UPDATE, DELETE ON audit_events FROM bridgeleads_app",
    # Profile & account (111): own avatar / sessions / email change. No DELETE.
    "GRANT SELECT, INSERT, UPDATE ON user_avatars, user_sessions, pending_email_changes "
    "TO bridgeleads_app",
    "REVOKE DELETE ON user_avatars, user_sessions, pending_email_changes FROM bridgeleads_app",
    "REVOKE INSERT, DELETE ON dialer_deliveries FROM bridgeleads_app",
    # contact_lookup_* (migration 101). Mirrors provision_rls_roles.sql. The
    # REVOKE must precede the column GRANT: a table-level REVOKE also clears
    # column privileges, so the other order leaves the API with no UPDATE.
    "REVOKE UPDATE ON contact_lookup_actions FROM bridgeleads_app",
    "GRANT SELECT, INSERT ON contact_lookup_actions TO bridgeleads_app",
    # dispatched_at only: the API never changes status (Codex round 19).
    "GRANT UPDATE (dispatched_at) ON contact_lookup_actions TO bridgeleads_app",
    "GRANT SELECT, INSERT ON contact_lookup_action_results TO bridgeleads_app",
    "GRANT SELECT, INSERT ON contact_lookup_action_events TO bridgeleads_app",
    "REVOKE UPDATE, DELETE ON contact_lookup_action_results FROM bridgeleads_app",
    "REVOKE UPDATE, DELETE ON contact_lookup_action_events FROM bridgeleads_app",
    "REVOKE DELETE ON contact_lookup_actions FROM bridgeleads_app",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bridgeleads_app",
    # system role
    "GRANT USAGE ON SCHEMA public TO bridgeleads_system",
    "GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO bridgeleads_system",
    # ...which includes contact_lookup_action_events, so take it back. The
    # event log is append-only for the WORKER TOO: history the writer can
    # edit is not evidence in a billing dispute. MUST stay after the blanket
    # grant above (Codex found this script; provision_rls_roles.sql alone was
    # not the whole story).
    "REVOKE UPDATE ON contact_lookup_action_events FROM bridgeleads_system",
    "GRANT DELETE ON county_records TO bridgeleads_system",
    "GRANT DELETE ON property_list_membership TO bridgeleads_system",
    # tasks.py releases a job's cross-job dedup claims on FIVE paths (trustee-sale
    # finalize failure, R2 upload failure, over-quota cap release, plan-cap
    # failure, enriched re-export failure) so leads that were never delivered and
    # never billed are not treated as duplicates forever. This line existed in
    # provision_rls_roles.sql but was MISSING here — and this script is what
    # actually provisioned prod, right after `REVOKE ALL ON delivered_records`
    # above. Result in production: bridgeleads_system held INSERT+UPDATE but not
    # DELETE, every release raised InsufficientPrivilege, and the over-quota
    # release (which runs INSIDE the plan-cap transaction) turned an ordinary
    # over-quota run into a FAILED job while stranding 16,761 dedup claims.
    "GRANT DELETE ON delivered_records TO bridgeleads_system",
    # H1: operator MFA reset (scripts/reset_user_mfa.py) deletes both MFA tables
    "GRANT DELETE ON mfa_backup_codes, mfa_break_glass_codes TO bridgeleads_system",
    # pending_registrations (074): worker dispatch SELECT/UPDATE (ALL TABLES) + purge DELETE
    "GRANT DELETE ON pending_registrations TO bridgeleads_system",
    # skip_trace_cache: the §7 retention sweep deletes aged rows. NULLing the PII
    # on `results` needs no new grant (the ALL TABLES UPDATE above covers it);
    # this is the only new privilege the retention task requires.
    "GRANT DELETE ON skip_trace_cache TO bridgeleads_system",
    # pending_skip_trace_rows (O-D): the claim's lost-race withdrawal deletes rows it
    # just inserted in the same transaction (skip_trace_claim.py). The only
    # pending-row DELETE in src, pinned by tests/test_worker_delete_grants.py.
    "GRANT DELETE ON pending_skip_trace_rows TO bridgeleads_system",
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
        -- contact_lookup_* (101), mirroring provision_rls_roles.sql.
        OR (privilege_type = 'UPDATE'
            AND table_name IN ('contact_lookup_actions',
                               'contact_lookup_action_results',
                               'contact_lookup_action_events'))
      )
"""

# role_table_grants cannot see COLUMN grants, so the check above passed while
# the API held no UPDATE on contact_lookup_actions at all (Codex round 19).
# has_*_privilege reports EFFECTIVE privilege for the named role, where
# information_schema.column_privileges is filtered by the current user's role
# membership. Returns one row per problem; an empty result means the API can
# update exactly dispatched_at and nothing else.
_VERIFY_APP_ACTION_COLUMNS = """
    SELECT 'table-wide UPDATE'
     WHERE has_table_privilege('bridgeleads_app', 'public.contact_lookup_actions', 'UPDATE')
    UNION ALL
    SELECT 'missing UPDATE (dispatched_at)'
     WHERE NOT has_column_privilege('bridgeleads_app', 'public.contact_lookup_actions',
                                    'dispatched_at', 'UPDATE')
    UNION ALL
    SELECT 'extra UPDATE (' || attname || ')' FROM pg_attribute
     WHERE attrelid = 'public.contact_lookup_actions'::regclass
       AND attnum > 0 AND NOT attisdropped AND attname <> 'dispatched_at'
       AND has_column_privilege('bridgeleads_app', 'public.contact_lookup_actions',
                                attname::text, 'UPDATE')
"""


# The app verify above is a NEGATIVE check: it fails when the app role holds a
# privilege it should not. Nothing asserted that the SYSTEM role still HOLDS the
# privileges the worker depends on, so a grant could go missing here and this
# script would still print "verified". That is exactly how DELETE on
# delivered_records was lost. This POSITIVE check closes that hole: every table
# the worker issues a DELETE against must be listed, and a missing grant is a
# hard failure rather than a silent runtime InsufficientPrivilege months later.
#
# Keep in sync with the worker's actual DELETE statements. Current sources:
#   delivered_records        tasks.py (dedup-claim release x5)
#   county_records           scheduler.py retention
#   property_list_membership overlap rollup prune
#   mfa_backup_codes         scripts/reset_user_mfa.py
#   mfa_break_glass_codes    scripts/reset_user_mfa.py
#   pending_registrations    hourly expired-row purge
#   skip_trace_cache         daily §7 retention purge (scheduler_helpers/retention.py)
#   pending_skip_trace_rows  the claim's lost-race withdrawal (skip_trace_claim.py), O-D
_SYSTEM_DELETE_TABLES = (
    "delivered_records",
    "county_records",
    "property_list_membership",
    "mfa_backup_codes",
    "mfa_break_glass_codes",
    "pending_registrations",
    "skip_trace_cache",
    "pending_skip_trace_rows",
)

_VERIFY_SYSTEM_GRANTS = """
    SELECT t.name FROM unnest(%s::text[]) AS t(name)
    WHERE NOT has_table_privilege('bridgeleads_system', 'public.' || t.name, 'DELETE')
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
            cur.execute(_VERIFY_APP_ACTION_COLUMNS)
            wrong = [r[0] for r in cur.fetchall()]
            if wrong:
                raise SystemExit(
                    "app role's UPDATE on contact_lookup_actions is wrong: "
                    + ", ".join(wrong) + " (expected exactly dispatched_at)"
                )
            cur.execute(_VERIFY_SYSTEM_GRANTS, (list(_SYSTEM_DELETE_TABLES),))
            missing = [r[0] for r in cur.fetchall()]
            if missing:
                raise SystemExit(
                    "system role is MISSING DELETE on: " + ", ".join(missing)
                    + " — the worker's cleanup paths would fail with InsufficientPrivilege"
                )
            print(
                f"  grants applied; app least-privilege verified (0 disallowed); "
                f"system DELETE verified on {len(_SYSTEM_DELETE_TABLES)} table(s)"
            )

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
