"""Account deletion purge: write fence + claim/progress/purge/complete functions (113).

Implements docs/product/account-deletion-retention-matrix.md (owner-signed 2026-10-06);
design + Codex review log in tasks/todo-account-deletion.md. Nothing calls these until
the P3b beat task ships, and ACCOUNT_DELETION_ENABLED stays off until the owner flips it.

  account_deletion_fence()   BEFORE INSERT OR UPDATE row trigger (ENABLE ALWAYS) on every
                             table the purge deletes from or scrubs. Locks the owning users
                             row FOR KEY SHARE and refuses the write (BLD20) once the
                             account is purging/deleted; user_id may never change (BLD21).
                             KEY SHARE does not serialize a user's writers, but conflicts
                             with the FOR UPDATE the purge takes first: the purge waits for
                             every writer already past the fence, and every later writer
                             blocks, re-reads the row, sees `purging` and fails. Writes made
                             by the purge functions themselves (current_user =
                             bridgeleads_purge) pass. Billing ledgers are not fenced: late
                             billing evidence from in-flight work is kept, never purged.
  claim_account_deletion()   picks one due deletion through its USERS row (lock order users
                             -> account_deletions everywhere) and moves it pending -> purging,
                             or reclaims an expired lease with a new token.
  record_deletion_progress() the only writer of phase markers, Stripe state (compare-and-set)
                             and errors (backoff).
  purge_account_data()       the matrix, in bounded batches: returns true when nothing is
                             left; db_purged_at is set in the same transaction as the last
                             batch, so a committed purge is never redone.
  complete_account_deletion() purging -> deleted once every phase marker is set.

All four are SECURITY DEFINER, owned by bridgeleads_purge (NOLOGIN, migration 112), with a
fixed search_path, and executable only by bridgeleads_system (the beat worker).

Revision ID: 113
Revises: 112
Create Date: 2026-10-06
"""
from alembic import op
from sqlalchemy import text

revision = "113"
down_revision = "112"
branch_labels = None
depends_on = None

# Tables carrying user_id that the purge deletes from or scrubs (job_logs is fenced
# separately: it has no user_id of its own).
_FENCED = (
    "results", "jobs", "scraper_configs", "scraper_batches", "batch_runs",
    "notifications", "user_record_views", "property_list_membership",
    "dialer_deliveries", "delivered_records", "pending_skip_trace_rows",
    "skip_trace_queues", "user_sessions", "user_avatars", "pending_email_changes",
    "password_history", "mfa_backup_codes", "mfa_break_glass_codes",
)
# purge role privileges: (table, privileges). Table-level SELECT keeps the batched
# id-subqueries simple; UPDATE is column-level wherever the purge only scrubs.
_PURGE_GRANTS = (
    ("user_sessions", "SELECT, DELETE"),
    ("user_avatars", "SELECT, DELETE"),
    ("pending_email_changes", "SELECT, DELETE"),
    ("password_history", "SELECT, DELETE"),
    ("mfa_backup_codes", "SELECT, DELETE"),
    ("mfa_break_glass_codes", "SELECT, DELETE"),
    ("notifications", "SELECT, DELETE"),
    ("user_record_views", "SELECT, DELETE"),
    ("property_list_membership", "SELECT, DELETE"),
    ("dialer_deliveries", "SELECT, DELETE"),
    ("batch_runs", "SELECT, DELETE"),
    ("job_logs", "SELECT, DELETE"),
    ("skip_trace_cache", "SELECT, DELETE"),
    ("pending_registrations", "SELECT, DELETE"),
    ("results", "SELECT, UPDATE (party_name, heirs, legal_description, mailing_address, "
                "enrichment_data, phone, phone_type, phone_dnc_flag, email, phones, emails, "
                "owner_state, absentee_owner, out_of_state_owner, last_trace_outcome, "
                "skip_trace_subject_hash)"),
    ("pending_skip_trace_rows", "SELECT, UPDATE (first_name, last_name, mail_address, "
                                "mail_city, mail_state, mail_zip)"),
    ("skip_trace_queues", "SELECT, UPDATE (download_url, error_message)"),
    ("jobs", "SELECT, UPDATE (export_key, error_message)"),
    ("scraper_configs", "SELECT, UPDATE (name, fields, enrichment, schedule, deliver, "
                        "doc_types, include_living_owner_tod, active)"),
    ("scraper_batches", "SELECT, UPDATE (name, fields, enrichment, schedule, deliver, "
                        "delivery_mode, status)"),
    ("audit_events", "SELECT, UPDATE (detail)"),
    ("delivered_records", "SELECT"),
)
_WORKER_FUNCTIONS = (
    "claim_account_deletion(interval)",
    "record_deletion_progress(uuid, uuid, text, text, text, text)",
    "purge_account_data(uuid, uuid, text[], integer)",
    "complete_account_deletion(uuid, uuid)",
)
_API_ROLES = ("anon", "authenticated", "service_role", "bridgeleads_app")


def _guarded(role: str, stmt: str) -> str:
    return (f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN {stmt} "
            "END IF;")


_FENCE_SQL = """
CREATE FUNCTION public.account_deletion_fence() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_state text;
BEGIN
    IF current_user = 'bridgeleads_purge' THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.user_id IS DISTINCT FROM NEW.user_id THEN
        RAISE EXCEPTION 'user_id is immutable' USING ERRCODE = 'BLD21';
    END IF;
    SELECT u.deletion_state INTO v_state
      FROM public.users u WHERE u.id = NEW.user_id FOR KEY SHARE;
    -- Not found: the foreign key rejects the row anyway; do not mask its error.
    IF FOUND AND v_state IS NOT NULL AND v_state <> 'pending' THEN
        RAISE EXCEPTION 'account is being deleted' USING ERRCODE = 'BLD20';
    END IF;
    RETURN NEW;
END
$fn$;

CREATE FUNCTION public.account_deletion_fence_job_logs() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_state text;
BEGIN
    IF current_user = 'bridgeleads_purge' THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.job_id IS DISTINCT FROM NEW.job_id THEN
        RAISE EXCEPTION 'job_id is immutable' USING ERRCODE = 'BLD21';
    END IF;
    SELECT u.deletion_state INTO v_state
      FROM public.jobs j JOIN public.users u ON u.id = j.user_id
     WHERE j.id = NEW.job_id
       FOR KEY SHARE OF u;
    IF FOUND AND v_state IS NOT NULL AND v_state <> 'pending' THEN
        RAISE EXCEPTION 'account is being deleted' USING ERRCODE = 'BLD20';
    END IF;
    RETURN NEW;
END
$fn$;
"""

# Lock order in every function: the users row, then the account_deletions row.
_FUNCTIONS_SQL = r"""
CREATE FUNCTION public.claim_account_deletion(p_lease interval)
RETURNS TABLE (deletion_id uuid, user_id uuid, claim_token uuid, reclaimed boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
#variable_conflict use_column
DECLARE
    v_now timestamptz := clock_timestamp();
    v_uid uuid;
    v_row public.account_deletions%ROWTYPE;
    v_token uuid := gen_random_uuid();
BEGIN
    IF p_lease IS NULL OR p_lease <= interval '0' OR p_lease > interval '1 hour' THEN
        RAISE EXCEPTION 'lease must be between 0 and 1 hour' USING ERRCODE = 'BLD30';
    END IF;
    -- Candidate chosen and locked through the USERS row (users-first lock order);
    -- SKIP LOCKED so concurrent workers take different accounts.
    SELECT u.id INTO v_uid
      FROM public.users u
      JOIN public.account_deletions d ON d.user_id = u.id
     WHERE (d.next_attempt_at IS NULL OR d.next_attempt_at <= v_now)
       AND ((d.status = 'pending' AND d.purge_after <= v_now
             AND d.stripe_state IN ('cancel_set', 'not_applicable'))
         OR (d.status = 'purging' AND d.claimed_until < v_now))
     ORDER BY d.purge_after
     LIMIT 1
       FOR NO KEY UPDATE OF u SKIP LOCKED;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    SELECT * INTO v_row FROM public.account_deletions d
     WHERE d.user_id = v_uid AND d.status IN ('pending', 'purging') FOR UPDATE;
    -- Re-verify under both locks (a restore may have won in between).
    IF NOT FOUND
       OR NOT (v_row.next_attempt_at IS NULL OR v_row.next_attempt_at <= v_now)
       OR NOT ((v_row.status = 'pending' AND v_row.purge_after <= v_now
                AND v_row.stripe_state IN ('cancel_set', 'not_applicable'))
            OR (v_row.status = 'purging' AND v_row.claimed_until < v_now)) THEN
        RETURN;
    END IF;
    IF v_row.status = 'pending' THEN
        UPDATE public.users SET deletion_state = 'purging' WHERE id = v_uid;
        UPDATE public.account_deletions
           SET status = 'purging', claim_token = v_token, claimed_until = v_now + p_lease,
               attempts = attempts + 1, next_attempt_at = NULL
         WHERE id = v_row.id;
        RETURN QUERY SELECT v_row.id, v_uid, v_token, false;
    ELSE
        UPDATE public.account_deletions
           SET claim_token = v_token, claimed_until = v_now + p_lease,
               attempts = attempts + 1, next_attempt_at = NULL
         WHERE id = v_row.id;
        RETURN QUERY SELECT v_row.id, v_uid, v_token, true;
    END IF;
END
$fn$;

-- p_phase:
--   'stripe'  compare-and-set stripe_state p_from -> p_to, allowed only as
--             pending:   pending_cancel   -> cancel_set | not_applicable
--             restored:  pending_uncancel -> uncancel_set | not_applicable
--             completed: cancel_set | not_applicable -> customer_deleted
--             no token. Returns false when the row moved on (e.g. restored meanwhile).
--   'scheduled_email_sent'  pending only, no token.
--   'r2_first_sweep' | 'final_email_sent' | 'tombstoned' | 'r2_final_sweep'
--             purging + matching token + live lease. 'tombstoned' also releases the lease
--             and parks the row until 24 h after the first sweep, when the claim reclaims it
--             (new token) for the final sweep; no worker holds a lease for a day.
--   'error'   with a token: purging (releases the lease); without: pending, restored or
--             completed (Stripe/email failures). Sets last_error and exponential backoff.
CREATE FUNCTION public.record_deletion_progress(
    p_deletion_id uuid, p_claim_token uuid, p_phase text,
    p_from text DEFAULT NULL, p_to text DEFAULT NULL, p_error text DEFAULT NULL)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_uid uuid;
    v_row public.account_deletions%ROWTYPE;
    v_backoff interval;
BEGIN
    SELECT d.user_id INTO v_uid FROM public.account_deletions d WHERE d.id = p_deletion_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown deletion' USING ERRCODE = 'BLD31';
    END IF;
    PERFORM 1 FROM public.users u WHERE u.id = v_uid FOR NO KEY UPDATE;
    SELECT * INTO v_row FROM public.account_deletions d WHERE d.id = p_deletion_id FOR UPDATE;

    IF p_phase = 'stripe' THEN
        IF NOT ((v_row.status = 'pending' AND p_from = 'pending_cancel'
                 AND p_to IN ('cancel_set', 'not_applicable'))
             OR (v_row.status = 'restored' AND p_from = 'pending_uncancel'
                 AND p_to IN ('uncancel_set', 'not_applicable'))
             OR (v_row.status = 'completed' AND p_from IN ('cancel_set', 'not_applicable')
                 AND p_to = 'customer_deleted')) THEN
            RETURN false;
        END IF;
        UPDATE public.account_deletions SET stripe_state = p_to, last_error = NULL
         WHERE id = p_deletion_id AND stripe_state = p_from;
        RETURN FOUND;
    END IF;

    IF p_phase = 'scheduled_email_sent' THEN
        IF v_row.status <> 'pending' THEN
            RETURN false;
        END IF;
        UPDATE public.account_deletions
           SET scheduled_email_sent_at = COALESCE(scheduled_email_sent_at, v_now)
         WHERE id = p_deletion_id;
        RETURN true;
    END IF;

    IF p_phase = 'error' AND p_claim_token IS NULL THEN
        IF v_row.status NOT IN ('pending', 'restored', 'completed') THEN
            RAISE EXCEPTION 'a purging deletion reports errors with its token'
                USING ERRCODE = 'BLD32';
        END IF;
        v_backoff := LEAST(interval '1 hour', interval '1 minute' * power(2, LEAST(v_row.attempts, 6)));
        UPDATE public.account_deletions
           SET last_error = left(p_error, 500), attempts = attempts + 1,
               next_attempt_at = v_now + v_backoff
         WHERE id = p_deletion_id;
        RETURN true;
    END IF;

    -- Everything else belongs to the purge and needs the live claim.
    IF v_row.status <> 'purging' OR v_row.claim_token IS DISTINCT FROM p_claim_token
       OR v_row.claimed_until < v_now THEN
        RAISE EXCEPTION 'not the live claim on this deletion' USING ERRCODE = 'BLD33';
    END IF;
    CASE p_phase
        WHEN 'r2_first_sweep' THEN
            UPDATE public.account_deletions
               SET r2_first_sweep_at = COALESCE(r2_first_sweep_at, v_now) WHERE id = p_deletion_id;
        WHEN 'final_email_sent' THEN
            IF v_row.db_purged_at IS NULL THEN
                RAISE EXCEPTION 'final email before the data purge' USING ERRCODE = 'BLD34';
            END IF;
            UPDATE public.account_deletions
               SET final_email_sent_at = COALESCE(final_email_sent_at, v_now) WHERE id = p_deletion_id;
        WHEN 'tombstoned' THEN
            IF v_row.final_email_sent_at IS NULL THEN
                RAISE EXCEPTION 'tombstone before the final email' USING ERRCODE = 'BLD34';
            END IF;
            UPDATE public.account_deletions
               SET tombstoned_at = COALESCE(tombstoned_at, v_now), claimed_until = v_now,
                   next_attempt_at = r2_first_sweep_at + interval '24 hours'
             WHERE id = p_deletion_id;
        WHEN 'r2_final_sweep' THEN
            IF v_row.tombstoned_at IS NULL
               OR v_row.r2_first_sweep_at > v_now - interval '24 hours' THEN
                RAISE EXCEPTION 'final sweep needs the tombstone and a 24 h gap'
                    USING ERRCODE = 'BLD34';
            END IF;
            UPDATE public.account_deletions
               SET r2_final_sweep_at = COALESCE(r2_final_sweep_at, v_now) WHERE id = p_deletion_id;
        WHEN 'error' THEN
            v_backoff := LEAST(interval '1 hour', interval '1 minute' * power(2, LEAST(v_row.attempts, 6)));
            UPDATE public.account_deletions
               SET last_error = left(p_error, 500), next_attempt_at = v_now + v_backoff,
                   claimed_until = v_now
             WHERE id = p_deletion_id;
        ELSE
            RAISE EXCEPTION 'unknown phase %', p_phase USING ERRCODE = 'BLD35';
    END CASE;
    RETURN true;
END
$fn$;

CREATE FUNCTION public.purge_account_data(
    p_deletion_id uuid, p_claim_token uuid, p_cache_keys text[], p_batch integer)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_now timestamptz := clock_timestamp();
    v_uid uuid;
    v_row public.account_deletions%ROWTYPE;
    v_hmac text;
    v_trial timestamptz;
    v_n integer;
    v_more boolean := false;
BEGIN
    IF p_batch IS NULL OR p_batch < 1 OR p_batch > 20000 THEN
        RAISE EXCEPTION 'batch must be 1..20000' USING ERRCODE = 'BLD30';
    END IF;
    SELECT d.user_id INTO v_uid FROM public.account_deletions d WHERE d.id = p_deletion_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown deletion' USING ERRCODE = 'BLD31';
    END IF;
    -- FOR UPDATE (not NO KEY): waits for every writer already past the fence (they hold
    -- FOR KEY SHARE on this row) before anything is deleted.
    SELECT u.email_hmac, u.trial_consumed_at INTO v_hmac, v_trial
      FROM public.users u WHERE u.id = v_uid FOR UPDATE;
    SELECT * INTO v_row FROM public.account_deletions d WHERE d.id = p_deletion_id FOR UPDATE;
    IF v_row.status <> 'purging' OR v_row.claim_token IS DISTINCT FROM p_claim_token
       OR v_row.claimed_until < clock_timestamp() THEN
        RAISE EXCEPTION 'not the live claim on this deletion' USING ERRCODE = 'BLD33';
    END IF;
    IF v_row.db_purged_at IS NOT NULL THEN
        RETURN true;  -- already committed: never redone
    END IF;
    IF v_row.r2_first_sweep_at IS NULL THEN
        RAISE EXCEPTION 'R2 export files must be swept first' USING ERRCODE = 'BLD34';
    END IF;

    -- Own credentials and profile.
    DELETE FROM public.user_sessions WHERE user_id = v_uid;
    DELETE FROM public.user_avatars WHERE user_id = v_uid;
    DELETE FROM public.pending_email_changes WHERE user_id = v_uid;
    DELETE FROM public.password_history WHERE user_id = v_uid;
    DELETE FROM public.mfa_backup_codes WHERE user_id = v_uid;
    DELETE FROM public.mfa_break_glass_codes WHERE user_id = v_uid;
    DELETE FROM public.pending_registrations WHERE email_hmac = v_hmac;
    -- Operational data with no dependents.
    DELETE FROM public.notifications WHERE user_id = v_uid;
    DELETE FROM public.user_record_views WHERE user_id = v_uid;
    DELETE FROM public.dialer_deliveries WHERE user_id = v_uid;
    DELETE FROM public.batch_runs WHERE user_id = v_uid;
    DELETE FROM public.job_logs l USING public.jobs j WHERE l.job_id = j.id AND j.user_id = v_uid;
    DELETE FROM public.property_list_membership
     WHERE (user_id, record_type, property_key) IN (
        SELECT user_id, record_type, property_key FROM public.property_list_membership
         WHERE user_id = v_uid LIMIT p_batch);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_more := v_more OR v_n = p_batch;
    -- The user's cached vendor answers (keys are per user: lookup_subject_key(user_id, ...)).
    IF p_cache_keys IS NOT NULL THEN
        DELETE FROM public.skip_trace_cache WHERE address_hash = ANY(p_cache_keys);
    END IF;
    DELETE FROM public.skip_trace_cache c USING public.results r
     WHERE r.user_id = v_uid AND r.skip_trace_subject_hash IS NOT NULL
       AND c.address_hash = r.skip_trace_subject_hash;

    -- Skeletons: keep the rows the billing ledgers hang off, blank the personal data.
    UPDATE public.results
       SET party_name = NULL, heirs = NULL, legal_description = NULL, mailing_address = NULL,
           enrichment_data = NULL, phone = NULL, phone_type = NULL, phone_dnc_flag = NULL,
           email = NULL, phones = NULL, emails = NULL, owner_state = NULL,
           absentee_owner = NULL, out_of_state_owner = NULL, last_trace_outcome = NULL,
           skip_trace_subject_hash = NULL
     WHERE id IN (
        SELECT id FROM public.results
         WHERE user_id = v_uid
           AND (party_name IS NOT NULL OR heirs IS NOT NULL OR legal_description IS NOT NULL
             OR mailing_address IS NOT NULL OR enrichment_data IS NOT NULL OR phone IS NOT NULL
             OR phone_type IS NOT NULL OR phone_dnc_flag IS NOT NULL OR email IS NOT NULL
             OR phones IS NOT NULL OR emails IS NOT NULL OR owner_state IS NOT NULL
             OR absentee_owner IS NOT NULL OR out_of_state_owner IS NOT NULL
             OR last_trace_outcome IS NOT NULL OR skip_trace_subject_hash IS NOT NULL)
         LIMIT p_batch);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_more := v_more OR v_n = p_batch;
    UPDATE public.pending_skip_trace_rows
       SET first_name = NULL, last_name = NULL, mail_address = NULL, mail_city = NULL,
           mail_state = NULL, mail_zip = NULL
     WHERE user_id = v_uid;
    UPDATE public.skip_trace_queues SET download_url = NULL, error_message = NULL
     WHERE user_id = v_uid;
    UPDATE public.jobs SET export_key = NULL, error_message = NULL WHERE user_id = v_uid;
    UPDATE public.scraper_configs
       SET name = 'deleted', fields = '[]'::json, enrichment = '[]'::json,
           schedule = '{}'::json, deliver = '{}'::json, doc_types = NULL,
           include_living_owner_tod = NULL, active = false
     WHERE user_id = v_uid;
    UPDATE public.scraper_batches
       SET name = 'deleted', fields = '[]'::json, enrichment = '[]'::json,
           schedule = '{}'::json, deliver = '{}'::json, delivery_mode = 'everything',
           status = 'archived'
     WHERE user_id = v_uid;
    UPDATE public.audit_events SET detail = NULL WHERE user_id = v_uid AND detail IS NOT NULL;
    -- Trial-fraud exception (owner-approved): only for an account that used its trial;
    -- a repeat keeps the LONGER expiry.
    IF v_trial IS NOT NULL THEN
        INSERT INTO public.consumed_trial_emails (email_hmac, expires_at)
        VALUES (v_hmac, v_now + interval '2 years')
        ON CONFLICT (email_hmac) DO UPDATE
           SET expires_at = GREATEST(public.consumed_trial_emails.expires_at, EXCLUDED.expires_at);
    END IF;

    IF v_more THEN
        RETURN false;
    END IF;
    UPDATE public.account_deletions SET db_purged_at = v_now WHERE id = p_deletion_id;
    RETURN true;
END
$fn$;

CREATE FUNCTION public.complete_account_deletion(p_deletion_id uuid, p_claim_token uuid)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_uid uuid;
    v_row public.account_deletions%ROWTYPE;
BEGIN
    SELECT d.user_id INTO v_uid FROM public.account_deletions d WHERE d.id = p_deletion_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'unknown deletion' USING ERRCODE = 'BLD31';
    END IF;
    PERFORM 1 FROM public.users u WHERE u.id = v_uid FOR NO KEY UPDATE;
    SELECT * INTO v_row FROM public.account_deletions d WHERE d.id = p_deletion_id FOR UPDATE;
    IF v_row.status <> 'purging' OR v_row.claim_token IS DISTINCT FROM p_claim_token
       OR v_row.claimed_until < clock_timestamp() THEN
        RAISE EXCEPTION 'not the live claim on this deletion' USING ERRCODE = 'BLD33';
    END IF;
    IF v_row.db_purged_at IS NULL OR v_row.final_email_sent_at IS NULL
       OR v_row.tombstoned_at IS NULL OR v_row.r2_first_sweep_at IS NULL
       OR v_row.r2_final_sweep_at IS NULL
       OR v_row.r2_final_sweep_at < v_row.r2_first_sweep_at + interval '24 hours' THEN
        RAISE EXCEPTION 'deletion has unfinished phases' USING ERRCODE = 'BLD34';
    END IF;
    -- Audit rows written while purging (e.g. a refused sign-in) lose their detail too.
    UPDATE public.audit_events SET detail = NULL WHERE user_id = v_uid AND detail IS NOT NULL;
    UPDATE public.users SET deletion_state = 'deleted' WHERE id = v_uid;
    UPDATE public.account_deletions
       SET status = 'completed', completed_at = clock_timestamp(), claim_token = NULL,
           claimed_until = NULL, last_error = NULL, next_attempt_at = clock_timestamp()
     WHERE id = p_deletion_id;
END
$fn$;
"""


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute(_FENCE_SQL)
    for tbl in _FENCED:
        op.execute(
            f"CREATE TRIGGER account_deletion_fence BEFORE INSERT OR UPDATE ON public.{tbl} "
            "FOR EACH ROW EXECUTE FUNCTION public.account_deletion_fence()"
        )
        op.execute(f"ALTER TABLE public.{tbl} ENABLE ALWAYS TRIGGER account_deletion_fence")
    op.execute(
        "CREATE TRIGGER account_deletion_fence BEFORE INSERT OR UPDATE ON public.job_logs "
        "FOR EACH ROW EXECUTE FUNCTION public.account_deletion_fence_job_logs()"
    )
    op.execute("ALTER TABLE public.job_logs ENABLE ALWAYS TRIGGER account_deletion_fence")
    op.execute(_FUNCTIONS_SQL)

    grants = []
    for tbl, privs in _PURGE_GRANTS:
        grants.append(f"GRANT {privs} ON public.{tbl} TO bridgeleads_purge;")
        grants.append(f"DROP POLICY IF EXISTS {tbl}_purge ON public.{tbl};")
        grants.append(
            f"CREATE POLICY {tbl}_purge ON public.{tbl} FOR ALL TO bridgeleads_purge "
            "USING (true) WITH CHECK (true);"
        )
    fns = ", ".join(f"public.{f}" for f in _WORKER_FUNCTIONS)
    revokes = [f"REVOKE ALL ON FUNCTION {fns} FROM PUBLIC;"]
    revokes += [_guarded(r, f"REVOKE ALL ON FUNCTION {fns} FROM {r};") for r in _API_ROLES]
    alters = "\n".join(
        f"ALTER FUNCTION public.{f} OWNER TO bridgeleads_purge;" for f in _WORKER_FUNCTIONS
    )
    nl = "\n"
    op.execute(
        f"""
        DO $purge_grants$
        DECLARE
            v_super boolean;
        BEGIN
            {nl.join(grants)}
            GRANT SELECT (email_hmac, trial_consumed_at) ON public.users TO bridgeleads_purge;
            GRANT UPDATE (expires_at) ON public.consumed_trial_emails TO bridgeleads_purge;
            {nl.join(revokes)}
            {_guarded("bridgeleads_system", f"GRANT EXECUTE ON FUNCTION {fns} TO bridgeleads_system;")}

            -- Same temporary hand-over as 112: SET on the purge role + CREATE on public,
            -- both taken back in this transaction (a superuser needs neither).
            SELECT rolsuper INTO v_super FROM pg_roles WHERE rolname = current_user;
            IF NOT v_super THEN
                EXECUTE format('GRANT bridgeleads_purge TO %I WITH SET TRUE, INHERIT FALSE',
                               current_user);
            END IF;
            GRANT CREATE ON SCHEMA public TO bridgeleads_purge;
            {alters}
            REVOKE CREATE ON SCHEMA public FROM bridgeleads_purge;
            IF NOT v_super THEN
                EXECUTE format('REVOKE bridgeleads_purge FROM %I', current_user);
            END IF;
            IF EXISTS (SELECT 1 FROM pg_auth_members m
                       WHERE m.roleid = 'bridgeleads_purge'::regrole
                         AND (m.member <> current_user::regrole
                              OR m.set_option OR m.inherit_option)) THEN
                RAISE EXCEPTION 'unexpected membership in bridgeleads_purge';
            END IF;
        END
        $purge_grants$;
        """
    )


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    drops = " ".join(f"DROP FUNCTION IF EXISTS public.{f};" for f in _WORKER_FUNCTIONS)
    op.execute(
        f"""
        DO $purge_drop$
        DECLARE
            v_me text := current_user;
            v_super boolean;
        BEGIN
            SELECT rolsuper INTO v_super FROM pg_roles WHERE rolname = v_me;
            IF NOT v_super THEN
                EXECUTE format('GRANT bridgeleads_purge TO %I WITH SET TRUE, INHERIT FALSE',
                               v_me);
                SET LOCAL ROLE bridgeleads_purge;
            END IF;
            {drops}
            IF NOT v_super THEN
                RESET ROLE;
                EXECUTE format('REVOKE bridgeleads_purge FROM %I', v_me);
            END IF;
        END
        $purge_drop$;
        """
    )
    for tbl in (*_FENCED, "job_logs"):
        op.execute(f"DROP TRIGGER IF EXISTS account_deletion_fence ON public.{tbl}")
    op.execute("DROP FUNCTION IF EXISTS public.account_deletion_fence()")
    op.execute("DROP FUNCTION IF EXISTS public.account_deletion_fence_job_logs()")
    revokes = []
    for tbl, _ in _PURGE_GRANTS:
        revokes.append(f"DROP POLICY IF EXISTS {tbl}_purge ON public.{tbl};")
        revokes.append(f"REVOKE ALL ON public.{tbl} FROM bridgeleads_purge;")
    nl = "\n"
    op.execute(
        f"""
        DO $purge_revoke$
        BEGIN
            {nl.join(revokes)}
            REVOKE SELECT (email_hmac, trial_consumed_at) ON public.users FROM bridgeleads_purge;
            REVOKE UPDATE (expires_at) ON public.consumed_trial_emails FROM bridgeleads_purge;
        END
        $purge_revoke$;
        """
    )
