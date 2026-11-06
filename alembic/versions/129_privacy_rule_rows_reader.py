"""privacy_rule_rows(): the one way a worker may read suppression rules (129).

Phase 6 P4a (docs/legal/audit-2026-11/06-DESIGN-PRIVACY-REQUESTS-AND-SUPPRESSION.md,
section 6). The send paths (dialer, Tracerfy dispatch and ingest) authorize every row
against the rules immediately before sending, so the worker role must be able to load
them. Migration 125 revoked every privilege on privacy_suppressions from the runtime
roles and enabled RLS with no policy, and the production worker role
(bridgeleads_system) is NOBYPASSRLS. Both obvious fixes are unsafe:

  * no access: src/privacy/authorize.load_rule_snapshot raises, so every send fails
    closed and stops;
  * GRANT SELECT alone: RLS with no policy returns ZERO rows without an error, so the
    snapshot is empty and every row is allowed. That fails OPEN, silently.

So the read goes through a SECURITY DEFINER function owned by the migration role (the
table owner, which RLS does not apply to). It returns exactly the columns the loader
needs: never request_id, never anything from the request or payload tables. It is
STABLE, schema-qualified, with search_path pinned (the 029 pattern). EXECUTE is revoked
from PUBLIC and granted to bridgeleads_system only (guarded: CI has no runtime roles).
No table privilege and no policy changes.

EXPAND ONLY, shipped before any code that calls it: Railway runs migrations at API
boot while workers start concurrently.

Forward-only in production: rolling back is a code revert. downgrade() exists for test
databases.

Revision ID: 129
Revises: 128
Create Date: 2026-11-06
"""
from alembic import op
from sqlalchemy import text

revision = "129"
down_revision = "128"
branch_labels = None
depends_on = None

_RUNTIME_READER = "bridgeleads_system"


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute("""
        CREATE FUNCTION public.privacy_rule_rows()
        RETURNS TABLE (
            id text,
            rule_id text,
            anchor_kind text,
            anchor_hmac text,
            subject_kind text,
            subject_hmac text,
            scope text,
            key_version text,
            normalization_version text,
            state text,
            created_at timestamptz
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $fn$
            SELECT s.id::text, s.rule_id::text, s.anchor_kind::text, s.anchor_hmac::text,
                   s.subject_kind::text, s.subject_hmac::text, s.scope::text,
                   s.key_version::text, s.normalization_version::text, s.state::text,
                   s.created_at
            FROM public.privacy_suppressions s
        $fn$
    """)
    op.execute("REVOKE ALL ON FUNCTION public.privacy_rule_rows() FROM PUBLIC")
    op.execute(
        "DO $grant$ BEGIN "
        f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_RUNTIME_READER}') THEN "
        f"GRANT EXECUTE ON FUNCTION public.privacy_rule_rows() TO {_RUNTIME_READER}; "
        "END IF; END $grant$;"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.privacy_rule_rows()")
