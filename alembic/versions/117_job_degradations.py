"""job_degradations: a job reached DONE although a phase failed (117).

P0-07 (pre-launch audit, scenario 14). run_scrape_job has handlers that catch a
phase's failure, log it and carry on to DONE: enrichment, the NTS match, survivor
re-election, claim transfer, the skip-trace enqueue, the property_key write, the
membership upsert, the owner-flag recompute and the parcel-less claim release.
Billing counts only delivered rows, so nobody is overcharged, but the customer
silently gets fewer or less complete leads and nobody is told.

One row per (job, reason), written by the worker in the same transaction as the
customer's job-log warning and BEFORE the job's DONE write. The row is history: it
is never deleted and its reason never changes. The repair columns serve the
skip-trace re-enqueue beat (P0-07 6b), the only reason repaired automatically;
every other reason stays 'none' (alert + manual backfill).

Worker-only: the app role gets nothing (API exposure is a later, additive change).
No user_id: the job's owner is jobs.user_id, and rows go with their job (CASCADE).
Fixed codes only, never exception text (P0-04: it can echo PII).

Supabase default privileges grant ALL on every new table to anon / authenticated /
service_role; revoked here. Grants/policies are role-guarded (CI has no runtime
roles) and mirrored in scripts/provision_rls_roles.sql.

Revision ID: 117
Revises: 116
Create Date: 2026-10-11
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID

revision = "117"
down_revision = "116"
branch_labels = None
depends_on = None

# Keep in step with src/workers/tasks_helpers/degradation.py REASONS.
_REASONS = (
    "enrichment", "nts_match", "survivor_reelection",
    "claim_transfer", "skip_trace_enqueue", "property_key", "property_membership",
    "owner_flags", "claim_release",
)
_REPAIR_STATUSES = ("none", "pending", "repaired", "suppressed", "gave_up")
_WORKER_UPDATE_COLUMNS = "repair_status, attempts, next_attempt_at, last_error_type"


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _guarded(role: str, stmt: str) -> str:
    return (f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN {stmt} "
            "END IF;")


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.create_table(
        "job_degradations",
        sa.Column(
            "id", UUID(as_uuid=False), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "job_id", UUID(as_uuid=False),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("reason", sa.String(32), nullable=False),
        # The exception's class name only (e.g. 'OperationalError'), never its text.
        sa.Column("exc_type", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("repair_status", sa.String(16), nullable=False, server_default="none"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_type", sa.String(64), nullable=True),
        sa.UniqueConstraint("job_id", "reason", name="uq_job_degradations_job_reason"),
        sa.CheckConstraint(f"reason IN ({_in(_REASONS)})", name="ck_job_degradations_reason"),
        sa.CheckConstraint(
            f"repair_status IN ({_in(_REPAIR_STATUSES)})", name="ck_job_degradations_repair",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_job_degradations_attempts"),
    )
    # The repair beat's queue.
    op.create_index(
        "ix_job_degradations_due", "job_degradations", ["next_attempt_at"],
        postgresql_where=sa.text("repair_status = 'pending'"),
    )

    op.execute("ALTER TABLE public.job_degradations ENABLE ROW LEVEL SECURITY")
    revokes = ["REVOKE ALL ON public.job_degradations FROM PUBLIC;"]
    revokes += [_guarded(r, f"REVOKE ALL ON public.job_degradations FROM {r};")
                for r in ("anon", "authenticated", "service_role",
                          "bridgeleads_app", "bridgeleads_system")]
    nl = "\n"
    op.execute(
        f"""
        DO $degradation_grants$
        BEGIN
            {nl.join(revokes)}
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
                GRANT SELECT, INSERT ON public.job_degradations TO bridgeleads_system;
                GRANT UPDATE ({_WORKER_UPDATE_COLUMNS}) ON public.job_degradations
                    TO bridgeleads_system;
                DROP POLICY IF EXISTS job_degradations_system ON public.job_degradations;
                CREATE POLICY job_degradations_system ON public.job_degradations
                    FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true);
            END IF;
        END
        $degradation_grants$;
        """
    )


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.drop_table("job_degradations")
