"""Contact-lookup ledger: install the role-targeted RLS policies inline (131).

INCIDENT 2026-11-07. Migration 101 created contact_lookup_actions / _results / _events
with only the untargeted `<t>_user_isolation` policy and left the role-targeted
`_app` / `_system` policies to scripts/apply_rls_cutover_policies.sql, which was never
re-run in production. The worker and the reconciler run as bridgeleads_system
(NOBYPASSRLS) with no tenant GUC, so every action was invisible to them: the worker's
dispatching -> running CAS matched 0 rows and returned silently, and the reconciler's
30-minute expiry selected 0 rows. Every action ever confirmed sat in `dispatching`.
Nothing was bought (no pending rows, no Tracerfy credits).

This migration installs exactly the policies the cutover script defines for these three
tables, so a deploy can never again depend on a manual step:
  actions, results: app SELECT + INSERT (tenant GUC), system FOR ALL.
  actions only:     app UPDATE, tenant GUC AND status = 'dispatching' (the API's one
                    dispatched_at stamp; the column grant and 101's trigger bound the rest).
  events:           append-only for both roles: app SELECT + INSERT (GUC),
                    system SELECT + INSERT.

Role-guarded like 124: it replaces the untargeted policy only when BOTH runtime roles
exist (a cluster without them, CI, keeps 101's policy unchanged; ONE of them alone
fails the migration rather than recording a no-op). When the roles exist
it asserts its own postcondition, so a half-applied state fails the migration instead
of shipping. Idempotent (DROP IF EXISTS before every CREATE), one transaction.

Revision ID: 131
Revises: 130
Create Date: 2026-11-07
"""
from alembic import op
from sqlalchemy import text

revision = "131"
down_revision = "130"
branch_labels = None
depends_on = None

_GUC = "user_id = NULLIF(current_setting(''app.current_user_id'', true), '''')::uuid"

# (table, policy suffix, command, role, USING, WITH CHECK). None = clause omitted.
_POLICIES = (
    ("contact_lookup_actions", "app_select", "SELECT", "bridgeleads_app", _GUC, None),
    ("contact_lookup_actions", "app_insert", "INSERT", "bridgeleads_app", None, _GUC),
    ("contact_lookup_actions", "app_update", "UPDATE", "bridgeleads_app",
     f"{_GUC} AND status = ''dispatching''", f"{_GUC} AND status = ''dispatching''"),
    ("contact_lookup_actions", "system", "ALL", "bridgeleads_system", "true", "true"),
    ("contact_lookup_action_results", "app_select", "SELECT", "bridgeleads_app", _GUC, None),
    ("contact_lookup_action_results", "app_insert", "INSERT", "bridgeleads_app", None, _GUC),
    ("contact_lookup_action_results", "system", "ALL", "bridgeleads_system", "true", "true"),
    ("contact_lookup_action_events", "app_select", "SELECT", "bridgeleads_app", _GUC, None),
    ("contact_lookup_action_events", "app_insert", "INSERT", "bridgeleads_app", None, _GUC),
    ("contact_lookup_action_events", "system_select", "SELECT", "bridgeleads_system",
     "true", None),
    ("contact_lookup_action_events", "system_insert", "INSERT", "bridgeleads_system",
     None, "true"),
)
_TABLES = ("contact_lookup_actions", "contact_lookup_action_results",
           "contact_lookup_action_events")
# Names the cutover script may have left behind; dropped so nothing stale OR-combines.
_LEGACY = ("contact_lookup_action_events_system",)


def _create(t: str, suffix: str, cmd: str, role: str, using: str | None,
            check: str | None) -> str:
    name = f"{t}_{suffix}"
    clauses = (f" USING ({using})" if using else "") + (f" WITH CHECK ({check})" if check else "")
    return (f"EXECUTE 'DROP POLICY IF EXISTS {name} ON public.{t}'; "
            f"EXECUTE 'CREATE POLICY {name} ON public.{t} FOR {cmd} TO {role}{clauses}';")


# Public so the tests can apply exactly this body inside their own transaction.
UPGRADE_SQL = (
    "DO $contact_lookup_policies$ BEGIN "
    # One role without the other is a half-provisioned cluster: skipping would record
    # 131 as applied with the incident still live, so refuse instead (Codex review).
    "IF (SELECT count(*) FROM pg_roles "
    "    WHERE rolname IN ('bridgeleads_app', 'bridgeleads_system')) = 1 THEN "
    "  RAISE EXCEPTION 'migration 131: only one of bridgeleads_app/bridgeleads_system "
    "exists; provision both (scripts/provision_rls_roles.sql) and re-run'; "
    "END IF; "
    "IF (SELECT count(*) FROM pg_roles "
    "    WHERE rolname IN ('bridgeleads_app', 'bridgeleads_system')) = 2 THEN "
    + " ".join(f"EXECUTE 'DROP POLICY IF EXISTS {t}_user_isolation ON public.{t}';"
               for t in _TABLES)
    + " ".join(f"EXECUTE 'DROP POLICY IF EXISTS {n} ON public.contact_lookup_action_events';"
               for n in _LEGACY)
    + " ".join(_create(*p) for p in _POLICIES)
    # Postcondition: the worker can see every table (the incident's exact failure).
    + " IF EXISTS (SELECT 1 FROM unnest(ARRAY['" + "','".join(_TABLES) + "']) t "
    "    WHERE NOT EXISTS (SELECT 1 FROM pg_policy p "
    "      WHERE p.polrelid = ('public.' || t)::regclass "
    "        AND 'bridgeleads_system'::regrole = ANY(p.polroles) "
    "        AND p.polcmd IN ('r', '*'))) THEN "
    "  RAISE EXCEPTION 'migration 131: a contact_lookup table has no system SELECT policy'; "
    "END IF; "
    "END IF; END $contact_lookup_policies$;"
)

DOWNGRADE_SQL = (
    "DO $contact_lookup_policies_down$ BEGIN "
    + " ".join(f"EXECUTE 'DROP POLICY IF EXISTS {t}_{s} ON public.{t}';"
               for t, s, *_ in _POLICIES)
    + " ".join(f"EXECUTE 'DROP POLICY IF EXISTS {t}_user_isolation ON public.{t}'; "
               f"EXECUTE 'CREATE POLICY {t}_user_isolation ON public.{t} USING ({_GUC})';"
               for t in _TABLES)
    + " END $contact_lookup_policies_down$;"
)


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute(text(UPGRADE_SQL))


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute(text(DOWNGRADE_SQL))
