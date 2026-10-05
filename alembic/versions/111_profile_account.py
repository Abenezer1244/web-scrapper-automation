"""Profile & account: avatar, timezone, sessions, email change, own security events (111).

  users.timezone          IANA zone id the user picked (display only). NULL = never
                          set. Schedules stay UTC; nothing reads this to schedule.
  user_avatars            one row per user: the server re-encoded 256px WebP, never
                          the upload. A separate table so `users` reads never load
                          image bytes. Removal NULLs `image` (the app role has no
                          DELETE anywhere); `version` changes on every upload so a
                          cached image can never outlive a replacement.
  user_sessions           one row per login session family (`fam` in the JWTs). Lists
                          the user's devices, carries the DB-authoritative revoke
                          (Redis family markers can be evicted) and the absolute
                          30-day session lifetime enforced at refresh.
  pending_email_changes   a requested email change awaiting proof of the new address.
                          Never updated in place to a different address: every request
                          is a NEW row and the previous pending row is superseded, so
                          an older link can never confirm a newer address. After
                          confirmation the row is also the outbox for the old-address
                          notice and the Stripe customer email sync (notice_state /
                          stripe_state), drained by beat like pending_registrations.
  audit_events            the app role may now SELECT its OWN rows (Settings >
                          Security "recent activity"). Still no app UPDATE/DELETE.

Additive only: nullable column + new tables + a grant/policy. Old code ignores all of it.

RLS: each new table gets the untargeted GUC isolation policy inline (the 065 pattern) so
it is isolated from creation; the role-targeted policies live in
scripts/apply_rls_cutover_policies.sql and the grants in scripts/provision_rls_roles.sql,
mirrored inline here (role-guarded, so CI with no provisioned roles is a no-op).

Revision ID: 111
Revises: 110
Create Date: 2026-10-05
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID

revision = "111"
down_revision = "110"
branch_labels = None
depends_on = None

_GUC_PREDICATE = (
    "user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid"
)
_NEW_TABLES = ("user_avatars", "user_sessions", "pending_email_changes")


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.add_column("users", sa.Column("timezone", sa.String(64), nullable=True))

    op.create_table(
        "user_avatars",
        sa.Column(
            "user_id", UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column("image", sa.LargeBinary(), nullable=True),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        # Belt to the upload limit: a re-encoded 256px WebP is ~10-40 KB.
        sa.CheckConstraint(
            "image IS NULL OR octet_length(image) <= 262144", name="ck_user_avatars_size"
        ),
    )

    op.create_table(
        "user_sessions",
        sa.Column("id", sa.String(32), primary_key=True),  # the JWT `fam`
        sa.Column(
            "user_id", UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("user_agent", sa.String(256), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_user_sessions_user_seen", "user_sessions",
        ["user_id", sa.text("last_seen_at DESC")],
    )

    op.create_table(
        "pending_email_changes",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id", UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("new_email", sa.Text(), nullable=False),  # EncryptedString
        sa.Column("new_email_hmac", sa.String(64), nullable=False),
        sa.Column("old_email", sa.Text(), nullable=True),  # EncryptedString, set at confirm
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notice_state", sa.String(16), nullable=True),
        sa.Column("stripe_state", sa.String(16), nullable=True),
        sa.Column("outbox_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_outbox_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'superseded', 'confirmed')",
            name="ck_pending_email_changes_status",
        ),
        sa.CheckConstraint(
            "notice_state IS NULL OR notice_state IN ('pending', 'sent', 'failed')",
            name="ck_pending_email_changes_notice_state",
        ),
        sa.CheckConstraint(
            "stripe_state IS NULL OR stripe_state IN ('pending', 'synced', 'skipped', 'failed')",
            name="ck_pending_email_changes_stripe_state",
        ),
    )
    op.create_index(
        "ix_pending_email_changes_user", "pending_email_changes", ["user_id"]
    )
    # At most one live request per user: two concurrent requests cannot both stay
    # 'pending' (the loser's INSERT fails instead of leaving two confirmable links).
    op.create_index(
        "uq_pending_email_changes_one_pending", "pending_email_changes", ["user_id"],
        unique=True, postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_pending_email_changes_outbox", "pending_email_changes",
        ["next_outbox_attempt_at"],
        postgresql_where=sa.text("notice_state = 'pending' OR stripe_state = 'pending'"),
    )
    # Own-activity feed reads (user_id, newest first).
    op.create_index(
        "ix_audit_events_user_created", "audit_events",
        ["user_id", sa.text("created_at DESC")],
    )

    for tbl in _NEW_TABLES:
        op.execute(f"ALTER TABLE public.{tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {tbl}_user_isolation ON public.{tbl} USING ({_GUC_PREDICATE})"
        )

    # Grants mirror scripts/provision_rls_roles.sql exactly. The app never DELETEs
    # (removal = NULL image / revoked_at / status). The system role drains the
    # email-change outbox (SELECT, UPDATE) and needs nothing on the other two.
    op.execute(
        f"""
        DO $profile_grants$
        BEGIN
            REVOKE ALL ON public.user_avatars, public.user_sessions,
                          public.pending_email_changes FROM PUBLIC;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL ON public.user_avatars, public.user_sessions,
                              public.pending_email_changes FROM anon;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL ON public.user_avatars, public.user_sessions,
                              public.pending_email_changes FROM authenticated;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_app') THEN
                GRANT SELECT, INSERT, UPDATE ON public.user_avatars, public.user_sessions,
                      public.pending_email_changes TO bridgeleads_app;
                REVOKE DELETE ON public.user_avatars, public.user_sessions,
                       public.pending_email_changes, public.audit_events FROM bridgeleads_app;
                GRANT SELECT ON public.audit_events TO bridgeleads_app;
                DROP POLICY IF EXISTS audit_events_app_select_own ON public.audit_events;
                CREATE POLICY audit_events_app_select_own ON public.audit_events
                    FOR SELECT TO bridgeleads_app USING ({_GUC_PREDICATE});
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
                GRANT SELECT, UPDATE ON public.pending_email_changes TO bridgeleads_system;
                -- The beat outbox drains confirmed rows cross-tenant with no GUC, so
                -- the untargeted isolation policy alone would hide every row from it.
                -- Created here (not only in apply_rls_cutover_policies.sql) so the
                -- outbox works the moment this migration lands; that script
                -- re-creates the same policy idempotently.
                DROP POLICY IF EXISTS pending_email_changes_system ON public.pending_email_changes;
                CREATE POLICY pending_email_changes_system ON public.pending_email_changes
                    FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true);
            END IF;
        END
        $profile_grants$;
        """
    )


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute("DROP POLICY IF EXISTS audit_events_app_select_own ON public.audit_events")
    op.execute(
        """
        DO $profile_revoke$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_app') THEN
                REVOKE SELECT ON public.audit_events FROM bridgeleads_app;
            END IF;
        END
        $profile_revoke$;
        """
    )
    op.drop_index("ix_audit_events_user_created", table_name="audit_events")
    for tbl in reversed(_NEW_TABLES):
        op.execute(f"DROP POLICY IF EXISTS {tbl}_user_isolation ON public.{tbl}")
    op.drop_table("pending_email_changes")
    op.drop_index("ix_user_sessions_user_seen", table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_table("user_avatars")
    op.drop_column("users", "timezone")
