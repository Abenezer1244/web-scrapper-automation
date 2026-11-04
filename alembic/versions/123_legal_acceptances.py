"""Legal acceptances: the append-only record of Terms / Privacy acceptance (123).

One row per document a person accepted at signup (and, later, at re-acceptance when a
document changes). Design and Codex consult: tasks/todo-compliance-audit-2026-11.md,
Phase 3. Schema only: no ORM mapping or writer ships with this (schema-first, as 115).

  Until this table the signup checkbox was enforced only in the browser and nothing was
  stored, so there was no evidence of who accepted which version, or when (audit
  2026-11 P1-4).

  Append-only: nobody holds UPDATE or DELETE, so a re-acceptance adds a row and never
  overwrites the earlier one. UNIQUE(user_id, document, version) makes a retried signup
  idempotent. `accepted_at` is the DATABASE clock: the app role has no INSERT grant on
  it, so a client- or app-supplied time can never be written. `version` is the
  document's published version, which the API validates against its own registry.

  Kept after an account purge (not in the purge matrix), like account_exports and the
  billing ledgers: it is evidence of the contract and carries ids, document names,
  versions and timestamps only. No IP or user agent is stored (owner decision pending,
  audit 2026-11 B1-4).

  The 113 fence trigger is attached with no pinned columns: an INSERT for a purging or
  deleted owner raises BLD20, and user_id is immutable.

Supabase default privileges grant ALL on every new table to anon / authenticated /
service_role; revoked here. Grants/policies are role-guarded (CI has no runtime roles)
and mirrored in scripts/provision_rls_roles.sql.

Revision ID: 123
Revises: 122
Create Date: 2026-11-04
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID

revision = "123"
down_revision = "122"
branch_labels = None
depends_on = None

_GUC_PREDICATE = (
    "user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid"
)


def _guarded(role: str, stmt: str) -> str:
    return (f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN {stmt} "
            "END IF;")


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.create_table(
        "legal_acceptances",
        sa.Column(
            "id", UUID(as_uuid=False), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id", UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("document", sa.String(16), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        # How the acceptance was given: the legacy one-step signup, the verified
        # signup (the person who proved the address), or a later re-acceptance.
        sa.Column("flow", sa.String(16), nullable=False),
        sa.Column(
            "accepted_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint(
            "document IN ('terms', 'privacy')", name="ck_legal_acceptances_document"
        ),
        # A published "last updated" date, never free text.
        sa.CheckConstraint(
            "version ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'", name="ck_legal_acceptances_version"
        ),
        sa.CheckConstraint(
            "flow IN ('register', 'verify_email', 'reaccept')",
            name="ck_legal_acceptances_flow",
        ),
        sa.UniqueConstraint(
            "user_id", "document", "version", name="uq_legal_acceptances_user_doc_version"
        ),
    )

    op.execute(
        "CREATE TRIGGER zz_account_deletion_fence BEFORE INSERT OR UPDATE "
        "ON public.legal_acceptances FOR EACH ROW "
        "EXECUTE FUNCTION public.account_deletion_fence()"
    )
    op.execute(
        "ALTER TABLE public.legal_acceptances ENABLE ALWAYS TRIGGER zz_account_deletion_fence"
    )
    # Append-only by construction, not only by grants (Codex): no role, the owner
    # included, may rewrite an accepted version, flow or timestamp. DELETE stays
    # possible for the owner only, because the matrix's eventual removal of a
    # tombstoned users row cascades through here; no runtime role holds DELETE.
    op.execute(
        """
        CREATE FUNCTION public.legal_acceptances_immutable() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $fn$
        BEGIN
            RAISE EXCEPTION 'legal_acceptances rows are append-only'
                USING ERRCODE = 'BLD22';
        END
        $fn$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION public.legal_acceptances_immutable() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER legal_acceptances_no_update BEFORE UPDATE "
        "ON public.legal_acceptances FOR EACH ROW "
        "EXECUTE FUNCTION public.legal_acceptances_immutable()"
    )
    op.execute(
        "ALTER TABLE public.legal_acceptances ENABLE ALWAYS TRIGGER legal_acceptances_no_update"
    )

    op.execute("ALTER TABLE public.legal_acceptances ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY legal_acceptances_user_isolation ON public.legal_acceptances "
        f"USING ({_GUC_PREDICATE}) WITH CHECK ({_GUC_PREDICATE})"
    )
    revokes = ["REVOKE ALL ON public.legal_acceptances FROM PUBLIC;"]
    revokes += [_guarded(r, f"REVOKE ALL ON public.legal_acceptances FROM {r};")
                for r in ("anon", "authenticated", "service_role",
                          "bridgeleads_app", "bridgeleads_system")]
    nl = "\n"
    op.execute(
        f"""
        DO $legal_grants$
        BEGIN
            {nl.join(revokes)}
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_app') THEN
                GRANT SELECT ON public.legal_acceptances TO bridgeleads_app;
                GRANT INSERT (user_id, document, version, flow)
                    ON public.legal_acceptances TO bridgeleads_app;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
                GRANT SELECT ON public.legal_acceptances TO bridgeleads_system;
                DROP POLICY IF EXISTS legal_acceptances_system ON public.legal_acceptances;
                CREATE POLICY legal_acceptances_system ON public.legal_acceptances
                    FOR SELECT TO bridgeleads_system USING (true);
            END IF;
        END
        $legal_grants$;
        """
    )


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.drop_table("legal_acceptances")
    op.execute("DROP FUNCTION IF EXISTS public.legal_acceptances_immutable()")
