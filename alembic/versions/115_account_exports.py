"""Account data export: the account_exports table (115).

One row per "Export my data" request (P4; design docs/product/account-deletion-and-export.md
§3, plan + Codex review log tasks/todo-account-deletion.md). The row is the durable
24-hour limit, the status the Settings page shows, the beat worker's outbox (claim lease,
backoff) and the 7-day expiry. Schema only: no ORM mapping ships with this (schema-first).

  The ZIP lives at exports/{user_id}/account/{id}.zip, always DERIVED from the row, never
  stored: no row can point at another tenant's object, and the expiry sweep can delete it
  whatever state a crashed build left behind. That prefix is inside the P3 purge sweep.

  The app role may only INSERT a pending row for itself (column grant on user_id, RLS
  WITH CHECK on the session GUC) and read its own rows; every later change is the worker's.
  Nobody holds DELETE: after a purge the rows stay 24 months as the request log (owner,
  2026-10-07), carrying ids, timestamps and status only.

  The 113 fence trigger is attached with no pinned columns: an INSERT for a purging or
  deleted owner raises BLD20 and user_id is immutable. Publishing a finished export is
  ordered against a deletion request by the worker (users row FOR SHARE), not by a pin.

Supabase default privileges grant ALL on every new table to anon / authenticated /
service_role; revoked here. Grants/policies are role-guarded (CI has no runtime roles) and
mirrored in scripts/provision_rls_roles.sql.

Revision ID: 115
Revises: 114
Create Date: 2026-10-07
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID

revision = "115"
down_revision = "114"
branch_labels = None
depends_on = None

_GUC_PREDICATE = (
    "user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid"
)
_WORKER_COLUMNS = (
    "status, claim_id, claimed_until, next_attempt_at, attempts, size_bytes, ready_at, "
    "expires_at, email_sent_at, email_attempts, last_error"
)


def _guarded(role: str, stmt: str) -> str:
    return (f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN {stmt} "
            "END IF;")


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.create_table(
        "account_exports",
        sa.Column(
            "id", UUID(as_uuid=False), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id", UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        # Worker lease: a claim rotates claim_id; every later write matches it.
        sa.Column("claim_id", UUID(as_uuid=False), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_attempts", sa.Integer(), nullable=False, server_default="0"),
        # A fixed code, never exception text (it may echo PII).
        sa.Column("last_error", sa.String(64), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'building', 'ready', 'failed', 'expired')",
            name="ck_account_exports_status",
        ),
        sa.CheckConstraint(
            "status <> 'ready' OR (size_bytes IS NOT NULL AND ready_at IS NOT NULL "
            "AND expires_at IS NOT NULL)",
            name="ck_account_exports_ready",
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at = ready_at + interval '7 days'",
            name="ck_account_exports_expiry",
        ),
    )
    op.create_index(
        "ix_account_exports_user", "account_exports", ["user_id", "requested_at"]
    )
    # One export in progress per account: two concurrent requests cannot both queue.
    op.create_index(
        "uq_account_exports_one_open", "account_exports", ["user_id"],
        unique=True, postgresql_where=sa.text("status IN ('pending', 'building')"),
    )
    # The beat's work queue: builds owed, and ready exports still owing their email.
    op.create_index(
        "ix_account_exports_due", "account_exports", ["next_attempt_at"],
        postgresql_where=sa.text(
            "status IN ('pending', 'building') "
            "OR (status = 'ready' AND email_sent_at IS NULL)"
        ),
    )

    op.execute(
        "CREATE TRIGGER zz_account_deletion_fence BEFORE INSERT OR UPDATE "
        "ON public.account_exports FOR EACH ROW "
        "EXECUTE FUNCTION public.account_deletion_fence()"
    )
    op.execute(
        "ALTER TABLE public.account_exports ENABLE ALWAYS TRIGGER zz_account_deletion_fence"
    )

    op.execute("ALTER TABLE public.account_exports ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY account_exports_user_isolation ON public.account_exports "
        f"USING ({_GUC_PREDICATE}) WITH CHECK ({_GUC_PREDICATE})"
    )
    revokes = ["REVOKE ALL ON public.account_exports FROM PUBLIC;"]
    revokes += [_guarded(r, f"REVOKE ALL ON public.account_exports FROM {r};")
                for r in ("anon", "authenticated", "service_role",
                          "bridgeleads_app", "bridgeleads_system")]
    nl = "\n"
    op.execute(
        f"""
        DO $export_grants$
        BEGIN
            {nl.join(revokes)}
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_app') THEN
                GRANT SELECT ON public.account_exports TO bridgeleads_app;
                GRANT INSERT (user_id) ON public.account_exports TO bridgeleads_app;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
                GRANT SELECT ON public.account_exports TO bridgeleads_system;
                GRANT UPDATE ({_WORKER_COLUMNS}) ON public.account_exports
                    TO bridgeleads_system;
                DROP POLICY IF EXISTS account_exports_system ON public.account_exports;
                CREATE POLICY account_exports_system ON public.account_exports
                    FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true);
            END IF;
        END
        $export_grants$;
        """
    )


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.drop_table("account_exports")
