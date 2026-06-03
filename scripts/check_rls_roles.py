"""Read-only diagnostic: report the RLS posture of the live database.

Answers the two questions the red-team / Supabase-advisor work left open:
  1. Do BOTH connection roles (async DATABASE_URL + sync DATABASE_URL_SYNC)
     bypass RLS? (If they differ, the RLS_ENFORCE guard's sync-only check was
     insufficient — now fixed to check both, but this confirms the live state.)
  2. Which public tables still have RLS DISABLED (i.e. exposed to the Supabase
     anon PostgREST API)? Confirms migration 027 covered everything.

Runs ONLY read-only SELECTs against the Postgres catalog. No writes.

Usage:
    python scripts/check_rls_roles.py
"""
from __future__ import annotations

from sqlalchemy import create_engine, text

# Importing settings loads .env via the app's own loader (src/config/settings).
from src.config import settings

_ROLE_SQL = text(
    "SELECT current_user, rolbypassrls, rolsuper "
    "FROM pg_roles WHERE rolname = current_user"
)
# Tables in the public schema that have RLS turned OFF (relrowsecurity = false).
_RLS_OFF_SQL = text(
    "SELECT c.relname "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relrowsecurity = false "
    "ORDER BY c.relname"
)


def _sync_url(raw: str) -> str:
    """Normalize any DATABASE_URL form to a psycopg2 sync URL on the direct port."""
    return raw.replace("+asyncpg", "").replace(":6543/", ":5432/")


def _check(label: str, url: str) -> None:
    engine = create_engine(
        _sync_url(url), connect_args={"connect_timeout": 10}
    )
    try:
        with engine.connect() as conn:
            role, bypass, is_super = conn.execute(_ROLE_SQL).fetchone()
            verdict = "BYPASSES RLS" if (bypass or is_super) else "subject to RLS"
            print(f"[{label}] role={role}  bypassrls={bypass}  superuser={is_super}  -> {verdict}")
            if label == "sync (DATABASE_URL_SYNC)":
                rows = [r[0] for r in conn.execute(_RLS_OFF_SQL).fetchall()]
                if rows:
                    print(f"\n  Public tables with RLS DISABLED ({len(rows)}): {', '.join(rows)}")
                    print("  ^ these are reachable via the Supabase anon REST API — must be ENABLED.")
                else:
                    print("\n  All public tables have RLS ENABLED. ✓")
    finally:
        engine.dispose()


def main() -> None:
    print("=== BridgeLeads live RLS posture ===\n")
    _check("async (DATABASE_URL)", settings.DATABASE_URL)
    _check("sync (DATABASE_URL_SYNC)", settings.DATABASE_URL_SYNC)
    print(
        "\nNotes:\n"
        "  * Both roles BYPASS RLS  -> migration 027 (ENABLE RLS, no policy) is safe and\n"
        "    non-breaking; the public-API hole is the only exposure, now closed.\n"
        "  * If they DIFFER (one subject to RLS) -> do NOT enable RLS_ENFORCE until the\n"
        "    HIGH-2 cutover; the async API path could be denied.\n"
    )


if __name__ == "__main__":
    main()
