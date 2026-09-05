"""Detect (and optionally repair) missing DELETE grants for the worker role.

WHY THIS EXISTS
    scripts/provision_rls_roles.sql and scripts/_cutover_step2_grants_policies.py
    are supposed to be identical grant blocks. They drifted by one line: the
    cutover script — the one that actually provisioned prod — ran
    `REVOKE ALL ON delivered_records` and never re-granted DELETE.

    The worker then held INSERT+UPDATE but not DELETE, so all five
    dedup-claim-release paths in tasks.py raised InsufficientPrivilege. Those
    failures were caught and logged, so nothing surfaced. Consequences seen in
    production: an over-quota run FAILED outright (its release runs inside the
    plan-cap transaction) and 16,761 delivered_records claims were stranded,
    permanently suppressing those leads as duplicates for that user.

    A grant that is missing shows up as a caught exception months later. This
    turns it into an explicit check.

USAGE
    railway run python scripts/verify_worker_delete_grants.py            # report only
    railway run python scripts/verify_worker_delete_grants.py --apply    # repair

    Reporting works as the ordinary app/worker role. --apply needs a role that
    can GRANT (owner/admin): set DATABASE_URL_MIGRATE, else it refuses rather
    than half-applying.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text  # noqa: E402

# Every table the worker issues a DELETE against. Keep in sync with
# _SYSTEM_DELETE_TABLES in scripts/_cutover_step2_grants_policies.py — the test
# tests/test_worker_delete_grants.py asserts the two lists agree.
REQUIRED_DELETE_TABLES = (
    "delivered_records",
    "county_records",
    "property_list_membership",
    "mfa_backup_codes",
    "mfa_break_glass_codes",
    "pending_registrations",
)
ROLE = "bridgeleads_system"


def _dsn(for_write: bool) -> tuple[str, bool]:
    """(dsn, is_elevated). Repair requires the elevated DSN; reporting does not."""
    migrate = os.environ.get("DATABASE_URL_MIGRATE", "").strip()
    if for_write:
        if not migrate:
            raise SystemExit(
                "--apply needs DATABASE_URL_MIGRATE (a role that can GRANT). "
                "The app/worker role cannot grant to itself. Re-run with it set, "
                "or apply by hand:\n"
                + "\n".join(f"  GRANT DELETE ON {t} TO {ROLE};" for t in REQUIRED_DELETE_TABLES)
            )
        return migrate.replace("postgresql+psycopg2://", "postgresql://"), True
    dsn = migrate or os.environ.get("DATABASE_URL_SYNC", "") or os.environ.get("DATABASE_URL", "")
    if not dsn:
        raise SystemExit("no database URL in the environment")
    return (
        dsn.replace("postgresql+psycopg2://", "postgresql://")
           .replace("postgresql+asyncpg://", "postgresql://"),
        bool(migrate),
    )


def main() -> None:
    apply = "--apply" in sys.argv
    dsn, _ = _dsn(for_write=apply)
    engine = create_engine(dsn, future=True)
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": ROLE}
        ).first()
        if not exists:
            # NOT a clean bill of health: an operator running this against the
            # wrong database would otherwise read "nothing to check" as "fine".
            msg = f"role {ROLE} does not exist in this database"
            if "--allow-missing-role" in sys.argv:
                print(f"{msg} — skipping (--allow-missing-role)")
                return
            raise SystemExit(f"{msg} — wrong database? pass --allow-missing-role if expected.")

        missing = [
            t for t in REQUIRED_DELETE_TABLES
            if not conn.execute(
                text("SELECT has_table_privilege(:r, :t, 'DELETE')"), {"r": ROLE, "t": f"public.{t}"}
            ).scalar()
        ]
        for t in REQUIRED_DELETE_TABLES:
            print(f"  {'MISSING' if t in missing else 'ok     '}  DELETE on {t}")

        if not missing:
            print(f"\n{ROLE} holds DELETE on all {len(REQUIRED_DELETE_TABLES)} required tables.")
            return

        print(f"\n{len(missing)} MISSING grant(s): {', '.join(missing)}")
        print("Impact: the worker's cleanup paths for these tables fail with "
              "InsufficientPrivilege and are swallowed as caught exceptions.")
        if not apply:
            print("\nRe-run with --apply (and DATABASE_URL_MIGRATE set) to repair.")
            sys.exit(1)

        # Idempotent; each GRANT is independent so a partial failure is still progress.
        for t in missing:
            conn.execute(text(f"GRANT DELETE ON public.{t} TO {ROLE}"))
        conn.commit()
        still = [
            t for t in missing
            if not conn.execute(
                text("SELECT has_table_privilege(:r, :t, 'DELETE')"), {"r": ROLE, "t": f"public.{t}"}
            ).scalar()
        ]
        if still:
            raise SystemExit(f"grants applied but still missing: {', '.join(still)}")
        print(f"\nrepaired: granted DELETE on {', '.join(missing)}")


if __name__ == "__main__":
    main()
