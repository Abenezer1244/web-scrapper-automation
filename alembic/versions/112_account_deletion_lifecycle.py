"""Account deletion lifecycle: purge role, deletion state, request/restore (112).

Design: docs/product/account-deletion-and-export.md; plan + Codex review log:
tasks/todo-account-deletion.md (phase P1). Schema only: no ORM mapping ships with this
(schema-first), and nothing calls the functions until P2.

  bridgeleads_purge        NOLOGIN role that OWNS the lifecycle functions. Nothing logs in
                           as it and no app/worker role is a member of it, so the only way
                           to act as it is to call one of its SECURITY DEFINER functions.
                           Triggers check `current_user = 'bridgeleads_purge'`, which is
                           true only inside those functions.
  users.deletion_state     NULL | pending | purging | deleted. Changes ONLY through the
                           functions: a guard trigger rejects any other writer (the app and
                           worker roles hold table-wide UPDATE on users) and any transition
                           outside NULL->pending, pending->NULL, pending->purging,
                           purging->deleted.
  account_deletions        one row per deletion request (its id is the operation id): the
                           30-day deadline, the purge lease and the per-phase markers the
                           P3 purge resumes from. Only the purge role writes it.
  consumed_trial_emails    email HMACs of deleted accounts that had used a free trial, so
                           delete + re-register does not earn a second trial (fraud
                           exception, expires_at bounds it).
  request_account_deletion()  / restore_account_deletion()
                           act on the user bound to the session GUC `app.current_user_id`
                           (no user id parameter, so a caller can only act on itself).

Supabase default privileges grant ALL on every new table, and EXECUTE on every new
function, to anon / authenticated / service_role (service_role bypasses RLS). Everything
created here revokes them explicitly.

Grants/policies mirror scripts/provision_rls_roles.sql (role-guarded, so CI without the
app/system roles is a no-op for those parts). The purge role itself is created here
because the functions must have an owner the moment they exist.

Revision ID: 112
Revises: 111
Create Date: 2026-10-06
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID

revision = "112"
down_revision = "111"
branch_labels = None
depends_on = None

_GUC_PREDICATE = (
    "user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid"
)
_NEW_TABLES = ("account_deletions", "consumed_trial_emails")
_DEFINER_FUNCTIONS = ("request_account_deletion()", "restore_account_deletion()")
# Every role that must never touch the new objects directly.
_NO_ACCESS_ROLES = ("anon", "authenticated", "service_role", "bridgeleads_system")


def _revoke_all_sql(objects: str, kind: str) -> str:
    """REVOKE ALL on `objects` from PUBLIC and every _NO_ACCESS_ROLES role that exists."""
    stmts = [f"REVOKE ALL ON {kind} {objects} FROM PUBLIC;"]
    for role in _NO_ACCESS_ROLES:
        stmts.append(
            f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
            f"REVOKE ALL ON {kind} {objects} FROM {role}; END IF;"
        )
    return "\n".join(stmts)


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))

    # ── 1. The purge role. Fail closed if a same-named role exists with any power. ──
    op.execute(
        """
        DO $purge_role$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_purge') THEN
                CREATE ROLE bridgeleads_purge NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB
                    NOCREATEROLE NOINHERIT NOREPLICATION;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_purge'
                       AND (rolcanlogin OR rolsuper OR rolbypassrls OR rolcreatedb
                            OR rolcreaterole OR rolinherit OR rolreplication)) THEN
                RAISE EXCEPTION 'bridgeleads_purge exists with attributes it must not have';
            END IF;
        END
        $purge_role$;
        """
    )

    # ── 2. Schema. ──
    op.add_column("users", sa.Column("deletion_state", sa.String(16), nullable=True))
    op.create_check_constraint(
        "ck_users_deletion_state", "users",
        "deletion_state IS NULL OR deletion_state IN ('pending', 'purging', 'deleted')",
    )

    op.create_table(
        "account_deletions",
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
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=False),
        # P3 purge lease: a claim sets both; a reclaim after expiry rotates the token.
        sa.Column("claim_token", UUID(as_uuid=False), nullable=True),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        # Per-phase markers: each phase checks its own, so a crashed purge resumes.
        sa.Column("scheduled_email_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("r2_first_sweep_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("db_purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("final_email_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tombstoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("r2_final_sweep_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stripe_state", sa.String(32), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'purging', 'completed', 'restored')",
            name="ck_account_deletions_status",
        ),
        sa.CheckConstraint(
            "stripe_state IS NULL OR stripe_state IN ('pending_cancel', 'cancel_set', "
            "'pending_uncancel', 'uncancel_set', 'waiting_for_period_end', "
            "'customer_deleted', 'not_applicable', 'failed')",
            name="ck_account_deletions_stripe_state",
        ),
    )
    op.create_index("ix_account_deletions_user", "account_deletions", ["user_id"])
    # One open request per user: a second concurrent request cannot open a second row.
    op.create_index(
        "uq_account_deletions_one_open", "account_deletions", ["user_id"],
        unique=True, postgresql_where=sa.text("status IN ('pending', 'purging')"),
    )
    # The P3 beat task's work queue: open deletions, plus restored ones still owing a
    # Stripe un-cancel.
    op.create_index(
        "ix_account_deletions_due", "account_deletions", ["next_attempt_at"],
        postgresql_where=sa.text(
            "status IN ('pending', 'purging') OR stripe_state LIKE 'pending%'"
        ),
    )

    op.create_table(
        "consumed_trial_emails",
        sa.Column("email_hmac", sa.String(64), primary_key=True),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ── 3. Guard triggers (SECURITY INVOKER: inside a definer function current_user is
    #       the function owner, bridgeleads_purge; anywhere else it is the caller). ──
    op.execute(
        """
        CREATE FUNCTION public.users_deletion_state_guard() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $fn$
        BEGIN
            IF current_user <> 'bridgeleads_purge' THEN
                RAISE EXCEPTION 'users.deletion_state changes only through the account '
                                'deletion functions' USING ERRCODE = 'BLD10';
            END IF;
            IF TG_OP = 'INSERT' OR NOT (
                   (OLD.deletion_state IS NULL AND NEW.deletion_state = 'pending')
                OR (OLD.deletion_state = 'pending' AND NEW.deletion_state IS NULL)
                OR (OLD.deletion_state = 'pending' AND NEW.deletion_state = 'purging')
                OR (OLD.deletion_state = 'purging' AND NEW.deletion_state = 'deleted')
            ) THEN
                RAISE EXCEPTION 'illegal users.deletion_state transition'
                    USING ERRCODE = 'BLD11';
            END IF;
            RETURN NEW;
        END
        $fn$;

        CREATE TRIGGER users_deletion_state_guard_ins BEFORE INSERT ON public.users
            FOR EACH ROW WHEN (NEW.deletion_state IS NOT NULL)
            EXECUTE FUNCTION public.users_deletion_state_guard();
        CREATE TRIGGER users_deletion_state_guard_upd BEFORE UPDATE ON public.users
            FOR EACH ROW WHEN (OLD.deletion_state IS DISTINCT FROM NEW.deletion_state)
            EXECUTE FUNCTION public.users_deletion_state_guard();
        -- ALWAYS: also fires under session_replication_role = replica.
        ALTER TABLE public.users ENABLE ALWAYS TRIGGER users_deletion_state_guard_ins;
        ALTER TABLE public.users ENABLE ALWAYS TRIGGER users_deletion_state_guard_upd;

        CREATE FUNCTION public.account_deletions_guard() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $fn$
        BEGIN
            IF current_user <> 'bridgeleads_purge' THEN
                RAISE EXCEPTION 'account_deletions is written only by the account '
                                'deletion functions' USING ERRCODE = 'BLD10';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'pending' THEN
                    RAISE EXCEPTION 'a deletion request starts pending'
                        USING ERRCODE = 'BLD11';
                END IF;
            ELSIF OLD.user_id IS DISTINCT FROM NEW.user_id
               OR OLD.purge_after IS DISTINCT FROM NEW.purge_after
               OR (OLD.status IS DISTINCT FROM NEW.status AND NOT (
                      (OLD.status = 'pending' AND NEW.status IN ('purging', 'restored'))
                   OR (OLD.status = 'purging' AND NEW.status = 'completed'))) THEN
                RAISE EXCEPTION 'illegal account_deletions change' USING ERRCODE = 'BLD11';
            END IF;
            RETURN NEW;
        END
        $fn$;

        CREATE TRIGGER account_deletions_guard BEFORE INSERT OR UPDATE
            ON public.account_deletions
            FOR EACH ROW EXECUTE FUNCTION public.account_deletions_guard();
        ALTER TABLE public.account_deletions ENABLE ALWAYS TRIGGER account_deletions_guard;
        """
    )

    # ── 4. Lifecycle functions (SECURITY DEFINER; owner set to bridgeleads_purge below).
    #       Lock order everywhere: users row, then the account_deletions row. FOR NO KEY
    #       UPDATE (not FOR UPDATE) so request/restore never wait on ordinary writers. ──
    op.execute(
        """
        CREATE FUNCTION public.request_account_deletion()
        RETURNS TABLE (deletion_id uuid, purge_after timestamptz, created boolean)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
        #variable_conflict use_column
        DECLARE
            v_uid uuid := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            v_state text;
            v_active boolean;
            v_row public.account_deletions%ROWTYPE;
        BEGIN
            IF v_uid IS NULL THEN
                RAISE EXCEPTION 'no user bound to this session' USING ERRCODE = 'BLD04';
            END IF;
            SELECT u.deletion_state, u.is_active INTO v_state, v_active
              FROM public.users u WHERE u.id = v_uid FOR NO KEY UPDATE;
            IF NOT FOUND OR NOT v_active OR v_state = 'deleted' THEN
                RAISE EXCEPTION 'account not found or already deleted'
                    USING ERRCODE = 'BLD03';
            END IF;
            SELECT * INTO v_row FROM public.account_deletions d
             WHERE d.user_id = v_uid AND d.status IN ('pending', 'purging') FOR UPDATE;
            IF FOUND THEN
                IF v_row.status = 'purging' THEN
                    RAISE EXCEPTION 'deletion already started' USING ERRCODE = 'BLD01';
                END IF;
                -- Idempotent: a repeat request returns the open one, deadline unchanged.
                RETURN QUERY SELECT v_row.id, v_row.purge_after, false;
                RETURN;
            END IF;
            INSERT INTO public.account_deletions
                   (user_id, status, purge_after, stripe_state, next_attempt_at)
            VALUES (v_uid, 'pending', now() + interval '30 days', 'pending_cancel', now())
            RETURNING * INTO v_row;
            UPDATE public.users SET deletion_state = 'pending' WHERE id = v_uid;
            RETURN QUERY SELECT v_row.id, v_row.purge_after, true;
        END
        $fn$;

        CREATE FUNCTION public.restore_account_deletion()
        RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
        DECLARE
            v_uid uuid := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            v_row public.account_deletions%ROWTYPE;
        BEGIN
            IF v_uid IS NULL THEN
                RAISE EXCEPTION 'no user bound to this session' USING ERRCODE = 'BLD04';
            END IF;
            PERFORM 1 FROM public.users u WHERE u.id = v_uid FOR NO KEY UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'account not found' USING ERRCODE = 'BLD03';
            END IF;
            SELECT * INTO v_row FROM public.account_deletions d
             WHERE d.user_id = v_uid AND d.status IN ('pending', 'purging') FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'no pending deletion' USING ERRCODE = 'BLD02';
            END IF;
            IF v_row.status = 'purging' THEN
                RAISE EXCEPTION 'deletion already started' USING ERRCODE = 'BLD01';
            END IF;
            UPDATE public.account_deletions
               SET status = 'restored', restored_at = now(),
                   stripe_state = 'pending_uncancel', next_attempt_at = now()
             WHERE id = v_row.id;
            UPDATE public.users SET deletion_state = NULL WHERE id = v_uid;
            RETURN v_row.id;
        END
        $fn$;
        """
    )

    # ── 5. RLS. account_deletions: own rows via the GUC (app reads its status). Both
    #       tables + users: a purge-role policy for the definer functions. ──
    op.execute("ALTER TABLE public.account_deletions ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY account_deletions_user_isolation ON public.account_deletions "
        f"USING ({_GUC_PREDICATE})"
    )
    op.execute("ALTER TABLE public.consumed_trial_emails ENABLE ROW LEVEL SECURITY")
    for tbl in ("users", *_NEW_TABLES):
        op.execute(
            f"CREATE POLICY {tbl}_purge ON public.{tbl} FOR ALL TO bridgeleads_purge "
            "USING (true) WITH CHECK (true)"
        )

    # ── 6. Grants, then hand the functions to the purge role. ──
    revoke_tables = _revoke_all_sql(
        "public.account_deletions, public.consumed_trial_emails", "TABLE"
    )
    revoke_functions = _revoke_all_sql(
        ", ".join(f"public.{f}" for f in _DEFINER_FUNCTIONS), "FUNCTION"
    )
    op.execute(
        f"""
        DO $deletion_grants$
        DECLARE
            v_super boolean;
        BEGIN
            {revoke_tables}
            {revoke_functions}

            -- Purge role: exactly what the functions touch.
            GRANT USAGE ON SCHEMA public TO bridgeleads_purge;
            GRANT SELECT (id, is_active, deletion_state) ON public.users TO bridgeleads_purge;
            GRANT UPDATE (deletion_state) ON public.users TO bridgeleads_purge;
            GRANT SELECT, INSERT, UPDATE ON public.account_deletions TO bridgeleads_purge;
            GRANT SELECT, INSERT ON public.consumed_trial_emails TO bridgeleads_purge;

            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_app') THEN
                -- The app reads its own deletion row (GUC policy) and calls the two
                -- functions; it never writes either table.
                REVOKE ALL ON public.account_deletions, public.consumed_trial_emails
                    FROM bridgeleads_app;
                GRANT SELECT ON public.account_deletions TO bridgeleads_app;
                GRANT EXECUTE ON FUNCTION public.request_account_deletion(),
                      public.restore_account_deletion() TO bridgeleads_app;
                -- Registration (P3) checks consumed trials; hashes only.
                GRANT SELECT ON public.consumed_trial_emails TO bridgeleads_app;
                DROP POLICY IF EXISTS consumed_trial_emails_app_select
                    ON public.consumed_trial_emails;
                CREATE POLICY consumed_trial_emails_app_select
                    ON public.consumed_trial_emails FOR SELECT TO bridgeleads_app
                    USING (true);
            END IF;

            -- ALTER ... OWNER needs the current role to be able to SET ROLE to the new
            -- owner, and the new owner to have CREATE on the schema. Supabase's migration
            -- role is not a superuser, so grant both just for the hand-over, then take
            -- them back. A superuser (local/CI) needs neither.
            SELECT rolsuper INTO v_super FROM pg_roles WHERE rolname = current_user;
            IF NOT v_super THEN
                EXECUTE format('GRANT bridgeleads_purge TO %I WITH SET TRUE, INHERIT FALSE',
                               current_user);
            END IF;
            GRANT CREATE ON SCHEMA public TO bridgeleads_purge;
            ALTER FUNCTION public.request_account_deletion() OWNER TO bridgeleads_purge;
            ALTER FUNCTION public.restore_account_deletion() OWNER TO bridgeleads_purge;
            REVOKE CREATE ON SCHEMA public FROM bridgeleads_purge;
            IF NOT v_super THEN
                EXECUTE format('REVOKE SET OPTION FOR bridgeleads_purge FROM %I',
                               current_user);
            END IF;

            -- No runtime role may reach the purge role.
            IF EXISTS (SELECT 1 FROM pg_roles r
                       WHERE r.rolname IN ('bridgeleads_app', 'bridgeleads_system',
                                           'anon', 'authenticated', 'service_role')
                         AND pg_has_role(r.oid, 'bridgeleads_purge', 'MEMBER')) THEN
                RAISE EXCEPTION 'a runtime role is a member of bridgeleads_purge';
            END IF;
        END
        $deletion_grants$;
        """
    )


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    for fn in _DEFINER_FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS public.{fn}")
    op.execute("DROP TRIGGER IF EXISTS users_deletion_state_guard_ins ON public.users")
    op.execute("DROP TRIGGER IF EXISTS users_deletion_state_guard_upd ON public.users")
    op.execute("DROP FUNCTION IF EXISTS public.users_deletion_state_guard()")
    op.execute("DROP POLICY IF EXISTS users_purge ON public.users")
    op.drop_table("consumed_trial_emails")
    op.drop_table("account_deletions")
    op.execute("DROP FUNCTION IF EXISTS public.account_deletions_guard()")
    op.drop_constraint("ck_users_deletion_state", "users", type_="check")
    op.drop_column("users", "deletion_state")
    # The role is cluster-wide (other databases on the cluster may use it): revoke what
    # this migration granted in THIS database, keep the role.
    op.execute(
        """
        DO $purge_revoke$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_purge') THEN
                REVOKE ALL ON public.users FROM bridgeleads_purge;
                REVOKE USAGE ON SCHEMA public FROM bridgeleads_purge;
            END IF;
        END
        $purge_revoke$;
        """
    )
