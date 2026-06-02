"""Enable RLS on public tables exposed to the Supabase anon REST API.

REDTEAM / Supabase advisor `rls_disabled_in_public` (CRITICAL, 2026-05-31):
"Table publicly accessible — anyone with your project URL can read/edit/delete
all data because Row-Level Security is not enabled."

The exposure vector is Supabase's auto-generated PostgREST API (authenticated by
the `anon` key that ships in the frontend), NOT the FastAPI app. For that API,
RLS is the ONLY protection — there is no application-level `WHERE user_id`
filter in front of it. These public tables had no RLS, so the anon/authenticated
roles could read/write them directly, bypassing the app entirely:

  * users                   — emails, bcrypt password_hash, api_key_hash,
                              stripe_customer_id, is_admin (catastrophic)
  * county_records          — scraped homeowner PII (party_name, property_address);
                              RLS was EXPLICITLY disabled in migration 023:94
  * skip_trace_cache        — skip-trace PII (phone/email by address)
  * county_connectors       — connector config (tamperable)
  * skip_trace_meter_events — billing rows (migration 026, system table)

Fix: `ENABLE ROW LEVEL SECURITY` with NO policies = default-deny for every role
subject to RLS (the Supabase `anon`/`authenticated` roles), which locks the
public REST API out of these tables. The FastAPI app + Celery workers connect
with a BYPASSRLS Postgres role (see src/db/session.py check_rls_role_status +
migration 023), so RLS is skipped for them and NOTHING IN THE APP BREAKS.

Deliberately NO policies and NO FORCE here (Codex-reviewed):
  * default-deny is the whole point for the anon lockout;
  * FORCE is irrelevant to the non-owner anon/authenticated roles and would only
    risk surprising a future non-owner role — it belongs in the staged non-
    BYPASSRLS cutover (the deferred HIGH-2 work + `RLS_ENFORCE`), not this
    emergency lockout.

INDEPENDENT of `RLS_ENFORCE`: this migration protects the PUBLIC API and is safe
to ship now on the current BYPASSRLS role. `RLS_ENFORCE` is about making the APP
enforce RLS (a non-bypass role) and stays deferred.

OPERATOR VERIFICATION (Codex caveats):
  1. Confirm BOTH DATABASE_URL (async) and DATABASE_URL_SYNC (workers) roles have
     BYPASSRLS — check_rls_role_status only checks the sync engine. If they
     differ and one does NOT bypass, that path would be denied after this runs.
  2. county_records is read via get_rls_db in get_cached_records
     (src/api/routes/scrapers.py:~501-540); it works now under BYPASSRLS but
     will need a SELECT policy (shared-read for any authenticated session) when
     the non-bypass cutover lands.
"""
from alembic import op

revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


# Public app tables that lacked RLS and are therefore reachable via the anon
# PostgREST API. alembic_version is included defensively — no PII, but the
# Supabase linter flags any RLS-disabled public table and the migration role
# (owner/BYPASSRLS) is unaffected by enabling it.
_EXPOSED_TABLES = (
    "users",
    "county_records",
    "skip_trace_cache",
    "county_connectors",
    "skip_trace_meter_events",
    "alembic_version",
)


def upgrade() -> None:
    for table in _EXPOSED_TABLES:
        # ALTER TABLE IF EXISTS so the migration is safe on any environment
        # where a table happens to be absent. No policy => default-deny for
        # anon/authenticated; BYPASSRLS app role is unaffected.
        op.execute(f"ALTER TABLE IF EXISTS public.{table} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    # Revert to the prior state (RLS disabled). county_records returns to the
    # explicitly-disabled state from migration 023; the others to no-RLS.
    for table in _EXPOSED_TABLES:
        op.execute(f"ALTER TABLE IF EXISTS public.{table} DISABLE ROW LEVEL SECURITY")
