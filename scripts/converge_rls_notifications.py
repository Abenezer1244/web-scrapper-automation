"""Converge the notifications table (migration 065) into the live H1 RLS cutover.

Targeted single-table convergence — the same prudent pattern as
force_rls_nts_notices.py. The full operator scripts (apply_rls_cutover_policies.sql
+ apply_rls_force.sql) re-process all ~23 cutover tables, each taking a brief
ACCESS EXCLUSIVE lock on live tables (results, jobs, users …). notifications is the
ONLY table left un-converged after the 065 deploy, so doing just it achieves the
goal with zero lock blast radius on live api/scrape traffic (2026-06-13 lock-wedge
lesson). The SQL below is copied VERBATIM from scripts/apply_rls_cutover_policies.sql
(the notifications block) + the FORCE that apply_rls_force.sql applies (notifications
is in its tbls array), so the end-state is byte-identical to running the full scripts.

What it does (one atomic transaction):
- DROP the untargeted notifications_user_isolation policy (from migration 065).
- CREATE role-targeted notifications_app_select / _app_update / _system policies.
- ALTER TABLE … FORCE ROW LEVEL SECURITY.
Grants are NOT touched — migration 065's role-guarded GRANT block already set them
(app SELECT,UPDATE; system SELECT,INSERT,UPDATE); this script verifies them.

Why this is needed: prod api/worker connect as bridgeleads_app / bridgeleads_system
(NO bypassrls). Under only the untargeted policy, the GUC-less worker INSERT is
denied -> emit is swallowed (best-effort) and the feed never populates. The system
FOR ALL policy fixes that; FORCE aligns notifications with the other 23 tables.

Safety (Codex-reviewed GO, 2026-06-18):
- Connects as the table owner (postgres) via DATABASE_URL_SYNC — the admin DSN.
- Preflights: both cutover roles exist (bypassrls=false), RLS is ENABLED, grants are
  correct, and the table is in the expected pre/post state. Idempotent: re-running
  when already converged is a no-op.
- Transaction-local lock_timeout=3s + statement_timeout=30s: aborts rather than
  wedging behind live traffic. Single transaction -> no committed window where
  tenant isolation is absent (sessions see the old policy, or block then see the new).

  python scripts/converge_rls_notifications.py            # dry run (checks only)
  python scripts/converge_rls_notifications.py --apply    # apply the convergence
"""
import argparse
import os
import sys

sys.path.insert(0, ".")

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

load_dotenv()

# Verbatim from scripts/apply_rls_cutover_policies.sql (notifications block) + the
# FORCE from apply_rls_force.sql. Executed statement-by-statement in one txn.
_CONVERGE_STATEMENTS = [
    "DROP POLICY IF EXISTS notifications_user_isolation ON public.notifications",
    "DROP POLICY IF EXISTS notifications_app_select ON public.notifications",
    """CREATE POLICY notifications_app_select ON public.notifications
        FOR SELECT TO bridgeleads_app
        USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)""",
    "DROP POLICY IF EXISTS notifications_app_update ON public.notifications",
    """CREATE POLICY notifications_app_update ON public.notifications
        FOR UPDATE TO bridgeleads_app
        USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)""",
    "DROP POLICY IF EXISTS notifications_system ON public.notifications",
    """CREATE POLICY notifications_system ON public.notifications
        FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true)""",
    "ALTER TABLE public.notifications FORCE ROW LEVEL SECURITY",
]

_EXPECTED_POLICIES = {
    "notifications_app_select",
    "notifications_app_update",
    "notifications_system",
}


def _policies(conn) -> set:
    return {
        r[0]
        for r in conn.execute(text(
            "SELECT policyname FROM pg_policies "
            "WHERE schemaname='public' AND tablename='notifications'"
        ))
    }


def _state(conn) -> dict:
    row = conn.execute(text(
        "SELECT relrowsecurity, relforcerowsecurity, pg_get_userbyid(relowner) "
        "FROM pg_class WHERE oid='public.notifications'::regclass"
    )).first()
    return {"rls_enabled": row[0], "forced": row[1], "owner": row[2]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="apply the convergence (default: dry-run checks only)")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL_SYNC")
    if not dsn:
        print("ERROR: DATABASE_URL_SYNC not set in .env")
        sys.exit(1)

    engine = create_engine(dsn, future=True)
    with engine.connect() as conn:
        who = conn.execute(text("SELECT current_user")).scalar()
        roles = {
            r[0]: r[1] for r in conn.execute(text(
                "SELECT rolname, rolbypassrls FROM pg_roles "
                "WHERE rolname IN ('bridgeleads_app','bridgeleads_system')"
            ))
        }
        st = _state(conn)
        pols = _policies(conn)
        grants = {
            (r[0], r[1])
            for r in conn.execute(text(
                "SELECT grantee, privilege_type FROM information_schema.role_table_grants "
                "WHERE table_name='notifications' "
                "AND grantee IN ('bridgeleads_app','bridgeleads_system')"
            ))
        }
        print(f"connected_as={who}")
        print(f"roles={roles}")
        print(f"state={st}")
        print(f"policies_before={sorted(pols)}")
        print(f"grants={sorted(grants)}")

        # ---- Preflight guards (fail closed) ----
        if "bridgeleads_app" not in roles or "bridgeleads_system" not in roles:
            print("ABORT: cutover roles missing — run provision_rls_roles.sql first.")
            sys.exit(1)
        if roles.get("bridgeleads_app") or roles.get("bridgeleads_system"):
            print("ABORT: a cutover role has BYPASSRLS — unexpected; investigate before forcing.")
            sys.exit(1)
        if not st["rls_enabled"]:
            print("ABORT: RLS is NOT enabled on notifications — FORCE/policies would be meaningless.")
            sys.exit(1)
        want_grants = {
            ("bridgeleads_app", "SELECT"), ("bridgeleads_app", "UPDATE"),
            ("bridgeleads_system", "SELECT"), ("bridgeleads_system", "INSERT"),
            ("bridgeleads_system", "UPDATE"),
        }
        if not want_grants.issubset(grants):
            print(f"ABORT: grants not as expected (mig 065 self-grant). missing={want_grants - grants}")
            sys.exit(1)
        if ("bridgeleads_app", "INSERT") in grants or ("bridgeleads_app", "DELETE") in grants:
            print("ABORT: app role holds INSERT/DELETE on notifications — least-privilege violated.")
            sys.exit(1)

        # ---- Idempotency: already converged? ----
        if pols == _EXPECTED_POLICIES and st["forced"]:
            print("Already converged (role-targeted policies present, untargeted gone, FORCEd). No-op.")
            return

        if not args.apply:
            print("\nDRY RUN — preflight passed. Re-run with --apply to converge.")
            print("Will run (one txn, lock_timeout=3s, statement_timeout=30s):")
            for s in _CONVERGE_STATEMENTS:
                print("  " + " ".join(s.split()))
            return

    # ---- Apply: one atomic transaction with bounded waits ----
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '3s'"))
        conn.execute(text("SET LOCAL statement_timeout = '30s'"))
        for s in _CONVERGE_STATEMENTS:
            conn.execute(text(s))

    # ---- Post-verify (fresh connection) ----
    with engine.connect() as conn:
        pols = _policies(conn)
        st = _state(conn)
        print("\nAPPLIED.")
        print(f"policies_after={sorted(pols)}")
        print(f"state_after={st}")
        ok = (
            pols == _EXPECTED_POLICIES
            and st["rls_enabled"]
            and st["forced"]
            and "notifications_user_isolation" not in pols
        )
        print("RESULT: " + ("OK — notifications converged into the RLS cutover." if ok
                            else "MISMATCH — verify manually!"))
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
