"""Fix the audit_events grant drift (incident 2026-06-13).

bridgeleads_app (api role) has the audit_events_app_insert RLS policy but NO
table-level GRANT, so every login logs "permission denied for table
audit_events" and the durable audit write silently fails. Re-apply the grant
that provision_rls_roles.sql intends (drifted in prod — table is migration 055,
added after initial role provisioning).

Only INSERT is needed (least-privilege on a security table): the companion code
fix sets audit_events.created_at client-side so the ORM emits no RETURNING, so no
SELECT privilege/policy is required. Revoke any superfluous SELECT. Idempotent.

  python scripts/fix_audit_events_grant.py            # show plan (dry run)
  python scripts/fix_audit_events_grant.py --apply
"""
import argparse
import os
import sys

sys.path.insert(0, ".")

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

_STMTS = [
    "GRANT INSERT ON public.audit_events TO bridgeleads_app",
    # Revoke the superfluous column SELECT — the code fix removed the RETURNING.
    "REVOKE SELECT (id, created_at) ON public.audit_events FROM bridgeleads_app",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    # DDL/GRANT needs an OWNER/DDL role. Prefer DATABASE_URL_MIGRATE (the privileged
    # migration DSN); fall back to DATABASE_URL_SYNC ONLY because on some operator
    # boxes that points at the postgres owner. In the documented cutover config
    # DATABASE_URL_SYNC = bridgeleads_system (DML-only) and CANNOT grant (Codex) —
    # the script verifies it can grant by connecting and checking the role below.
    dsn = os.environ.get("DATABASE_URL_MIGRATE") or os.environ.get("DATABASE_URL_SYNC")
    if not dsn:
        print("ERROR: set DATABASE_URL_MIGRATE (owner/DDL DSN) — neither it nor DATABASE_URL_SYNC is set")
        sys.exit(1)

    engine = create_engine(dsn, future=True)
    with engine.connect() as conn:
        who = conn.execute(text("SELECT current_user")).scalar()
        print(f"connected_as={who}")
        if who in ("bridgeleads_system", "bridgeleads_app"):
            print(f"ABORT: connected as {who} — a DML-only role that cannot GRANT. "
                  "Point DATABASE_URL_MIGRATE at the owner/DDL role.")
            sys.exit(1)
        for s in _STMTS:
            print(("APPLY: " if args.apply else "PLAN:  ") + s)
            if args.apply:
                conn.execute(text(s))
        if args.apply:
            conn.commit()
        # verify
        privs = conn.execute(text(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee='bridgeleads_app' AND table_name='audit_events'")).fetchall()
        cols = conn.execute(text(
            "SELECT column_name, privilege_type FROM information_schema.column_privileges "
            "WHERE grantee='bridgeleads_app' AND table_name='audit_events'")).fetchall()
        print("now TABLE privs:", [p[0] for p in privs] or "NONE")
        print("now COLUMN grants:", [(c[0], c[1]) for c in cols] or "NONE")


if __name__ == "__main__":
    main()
