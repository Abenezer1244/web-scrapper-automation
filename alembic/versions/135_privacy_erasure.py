"""Privacy erasure: progress table, erased/invalidated markers, the batch function (135).

Phase 6 P6a (plan tasks/todo-privacy-phase6-p6.md, design section 7, decision D3: a
deletion request blanks the person data and keeps a nameless property skeleton).

  results.privacy_erased_at        when the erasure blanked the row. The row stays
                                   flagged (privacy_suppressed_at), so every reader and
                                   writer already skips it.
  jobs.privacy_invalidated_at      the job's R2 export held an erased person: the object
  batch_runs.privacy_invalidated_at  is deleted and downloads render from the database
                                   (which excludes flagged rows) instead of R2.
  privacy_erasures                 one row per erased rule: phase markers, the scan
                                   cursor, the last error. Written only by the worker
                                   role; one driver at a time (an advisory lock).
  privacy_erase_batch()            erases one bounded batch the worker found with
                                   authorize() (the HMAC keys live in Python, not SQL).
  privacy_erasure_rule()           the one read of the privacy tables the purge role
                                   needs (is this an active deletion rule?), owned by the
                                   tables' owner like 129's reader: 125's "no RLS policy
                                   on the privacy tables" invariant holds.

The function is SECURITY DEFINER, owned by bridgeleads_purge (NOLOGIN, NOBYPASSRLS, 112),
search_path fixed, executable only by bridgeleads_system, as 113's purge functions.
It refuses unless the given rule row is ACTIVE, the rule was never lifted and its request
is a deletion. Migration 125's guard admits only 'pending' rows, so nothing can be erased
in production until P7 lifts it: dormant by construction.

Per batch, under a transaction advisory lock on the rule:
  results         locked in id order; accounts purging/deleted are skipped (their own
                  purge covers them); flagged with the rule if not yet; the person columns
                  blanked (113's list, so enrichment_data = NULL).
  skip_trace_cache  deleted by the rows' subject hashes plus the keys the worker computed
                  from their pending lookups (pending_row_subject_key).
  pending_skip_trace_rows  unsent ('queued') rows deleted; sent rows keep only the
                  property address (billing evidence).
  skip_trace_queues  download_url cleared on the batches that held those rows.
  dialer_deliveries  unsent ('pending'/'failed') rows skipped; delivered rows kept (ids).
  king_parcel_owners  matched parcels become tombstones (131).
  nts_notices     matched notices lose the grantor and go inactive (132).
  county_records  matched rows deleted (orphaned store).
  jobs / batch_runs  marked invalidated when they hold an erased row with an R2 export.

Downgrade refuses while any row was erased or invalidated: dropping the markers would
re-enable the R2 download of a file that still names an erased person.

Revision ID: 135
Revises: 134
Create Date: 2026-11-08
"""
from alembic import op
from sqlalchemy import text

revision = "135"
down_revision = "134"
branch_labels = None
depends_on = None

_FN = "privacy_erase_batch(uuid, uuid[], text[], text[], uuid[], uuid[])"
_API_ROLES = ("anon", "authenticated", "service_role", "bridgeleads_app")
# Privileges the function needs beyond what 113 granted the purge role.
_PURGE_GRANTS = (
    ("results", "UPDATE (privacy_suppressed_at, privacy_suppression_id, privacy_erased_at)"),
    ("dialer_deliveries", "UPDATE (status, last_error)"),
    ("pending_skip_trace_rows", "DELETE"),
    ("jobs", "UPDATE (privacy_invalidated_at)"),
    ("batch_runs", "UPDATE (privacy_invalidated_at)"),
    ("king_parcel_owners", "SELECT, UPDATE (owner_name, outcome, privacy_suppression_id)"),
    ("nts_notices", "SELECT, UPDATE (grantor, is_active, privacy_suppression_id)"),
    ("county_records", "SELECT, DELETE"),
)
# Downgrade revokes only what 135 added (113's grants on the same tables stay). The
# column privileges on 135's own columns go with the columns.
_DOWNGRADE_REVOKES = (
    ("king_parcel_owners", "ALL"), ("nts_notices", "ALL"), ("county_records", "ALL"),
    ("results", "UPDATE (privacy_suppressed_at, privacy_suppression_id)"),
    ("dialer_deliveries", "UPDATE (status, last_error)"),
    ("pending_skip_trace_rows", "DELETE"),
)
# Tables that had no purge-role policy before (113 covered the rest).
_NEW_POLICIES = ("king_parcel_owners", "nts_notices", "county_records")
_RULE_FN = "privacy_erasure_rule(uuid)"

# The privacy tables carry no RLS policy for any role (125); they are read only through
# definer functions owned by the migration role, which owns them with RLS not forced
# (as 129's privacy_rule_rows). This one answers a single question for the purge role:
# the rule id behind an ACTIVE, never-lifted row of a DELETION request, else NULL.
_RULE_FUNCTION_SQL = """
CREATE FUNCTION public.privacy_erasure_rule(p_rule_row uuid)
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    SELECT s.rule_id
      FROM public.privacy_suppressions s
      JOIN public.privacy_requests r ON r.id = s.request_id
     WHERE s.id = p_rule_row AND s.state = 'active' AND r.kind = 'delete'
       AND NOT EXISTS (SELECT 1 FROM public.privacy_suppressions l
                        WHERE l.rule_id = s.rule_id AND l.state = 'lifted')
$fn$;
"""

_FUNCTION_SQL = """
CREATE FUNCTION public.privacy_erase_batch(
    p_rule_row uuid, p_results uuid[], p_cache_keys text[], p_king text[],
    p_nts uuid[], p_county uuid[])
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_rule uuid;
    v_ids uuid[];
    v_jobs uuid[];
    v_hashes text[];
    v_counts jsonb := '{}'::jsonb;
    v_n integer;
BEGIN
    IF coalesce(cardinality(p_results), 0) > 500 OR coalesce(cardinality(p_cache_keys), 0) > 2000
       OR coalesce(cardinality(p_king), 0) > 500 OR coalesce(cardinality(p_nts), 0) > 500
       OR coalesce(cardinality(p_county), 0) > 500 THEN
        RAISE EXCEPTION 'privacy_erase_batch: batch too large' USING ERRCODE = 'BLD40';
    END IF;
    v_rule := public.privacy_erasure_rule(p_rule_row);
    IF v_rule IS NULL THEN
        RAISE EXCEPTION 'privacy_erase_batch: not an active deletion rule'
              USING ERRCODE = 'BLD41';
    END IF;
    -- One erasure per rule at a time; independent of any account's lock.
    PERFORM pg_advisory_xact_lock(7135, hashtext(v_rule::text));

    -- results: lock in id order; a purging/deleted account is its own purge's job.
    SELECT array_agg(x.id ORDER BY x.id), array_agg(x.h) FILTER (WHERE x.h IS NOT NULL)
      INTO v_ids, v_hashes
      FROM (SELECT r.id, r.skip_trace_subject_hash AS h
              FROM public.results r
              JOIN public.users u ON u.id = r.user_id
             WHERE r.id = ANY(coalesce(p_results, '{}'))
               AND coalesce(u.deletion_state, 'active') NOT IN ('purging', 'deleted')
             ORDER BY r.id
               FOR UPDATE OF r) x;
    v_ids := coalesce(v_ids, '{}');

    -- The jobs of the rows erased NOW: their exports are what the R2 sweep deletes.
    WITH erased AS (
    UPDATE public.results r SET
        privacy_suppressed_at = coalesce(r.privacy_suppressed_at, now()),
        privacy_suppression_id = coalesce(r.privacy_suppression_id, p_rule_row),
        privacy_erased_at = coalesce(r.privacy_erased_at, now()),
        party_name = NULL, heirs = NULL, legal_description = NULL, mailing_address = NULL,
        enrichment_data = NULL, phone = NULL, phone_type = NULL, phone_dnc_flag = NULL,
        email = NULL, phones = NULL, emails = NULL, owner_state = NULL,
        absentee_owner = NULL, out_of_state_owner = NULL, last_trace_outcome = NULL,
        skip_trace_subject_hash = NULL
     WHERE r.id = ANY(v_ids)
       -- Only rows with something left, so a clean second pass counts zero.
       AND (r.privacy_erased_at IS NULL OR r.privacy_suppressed_at IS NULL
            OR num_nonnulls(r.party_name, r.heirs, r.legal_description, r.mailing_address,
                            r.enrichment_data, r.phone, r.phone_type, r.phone_dnc_flag,
                            r.email, r.phones, r.emails, r.owner_state, r.absentee_owner,
                            r.out_of_state_owner, r.last_trace_outcome,
                            r.skip_trace_subject_hash) > 0)
    RETURNING r.job_id)
    SELECT coalesce(array_agg(DISTINCT erased.job_id), '{}'), count(*)
      INTO v_jobs, v_n FROM erased;
    v_counts := v_counts || jsonb_build_object('results', v_n);

    DELETE FROM public.skip_trace_cache
     WHERE address_hash = ANY(coalesce(v_hashes, '{}') || coalesce(p_cache_keys, '{}'));
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_counts := v_counts || jsonb_build_object('cache', v_n);

    UPDATE public.skip_trace_queues q SET download_url = NULL
     WHERE q.download_url IS NOT NULL
       AND q.tracerfy_queue_id IN (SELECT p.tracerfy_queue_id FROM public.pending_skip_trace_rows p
                     WHERE p.result_id = ANY(v_ids) AND p.tracerfy_queue_id IS NOT NULL);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_counts := v_counts || jsonb_build_object('queues', v_n);

    -- Unsent lookups ('queued': never sent, never billed) are deleted outright; rows
    -- that reached Tracerfy are billing evidence and keep only the property address.
    DELETE FROM public.pending_skip_trace_rows p
     WHERE p.result_id = ANY(v_ids) AND p.status = 'queued';
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_counts := v_counts || jsonb_build_object('pending_deleted', v_n);

    UPDATE public.pending_skip_trace_rows p SET
        first_name = NULL, last_name = NULL, mail_address = NULL, mail_city = NULL,
        mail_state = NULL, mail_zip = NULL
     WHERE p.result_id = ANY(v_ids)
       AND num_nonnulls(p.first_name, p.last_name, p.mail_address, p.mail_city,
                        p.mail_state, p.mail_zip) > 0;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_counts := v_counts || jsonb_build_object('pending_scrubbed', v_n);

    UPDATE public.dialer_deliveries d SET status = 'skipped', last_error = 'privacy_erased'
     WHERE d.result_id = ANY(v_ids) AND d.status IN ('pending', 'failed');
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_counts := v_counts || jsonb_build_object('dialer', v_n);

    -- Re-stamped on every erasure (not only the first), so each erasure's R2 sweep
    -- finds the exports it must delete by time.
    UPDATE public.jobs j SET privacy_invalidated_at = now()
     WHERE j.id = ANY(v_jobs) AND j.export_key IS NOT NULL;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_counts := v_counts || jsonb_build_object('jobs', v_n);

    UPDATE public.batch_runs b SET privacy_invalidated_at = now()
     WHERE b.combined_export_key IS NOT NULL
       AND b.child_job_ids::jsonb ?| CAST(v_jobs AS text[]);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_counts := v_counts || jsonb_build_object('batch_runs', v_n);

    UPDATE public.king_parcel_owners k SET
        owner_name = NULL, outcome = 'privacy_withheld', privacy_suppression_id = p_rule_row
     WHERE k.parcel_id = ANY(coalesce(p_king, '{}')) AND k.outcome <> 'privacy_withheld';
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_counts := v_counts || jsonb_build_object('king', v_n);

    UPDATE public.nts_notices n SET
        grantor = NULL, is_active = false, privacy_suppression_id = p_rule_row
     WHERE n.id = ANY(coalesce(p_nts, '{}')) AND n.privacy_suppression_id IS NULL;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_counts := v_counts || jsonb_build_object('nts', v_n);

    DELETE FROM public.county_records c WHERE c.id = ANY(coalesce(p_county, '{}'));
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_counts := v_counts || jsonb_build_object('county', v_n);

    RETURN v_counts;
END
$fn$;
"""


def _guarded(role: str, stmt: str) -> str:
    return (f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN {stmt} "
            "END IF;")


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    # ADD COLUMN without a default is catalog-only on these large tables.
    op.execute(text("ALTER TABLE public.results ADD COLUMN privacy_erased_at timestamptz"))
    op.execute(text("ALTER TABLE public.jobs ADD COLUMN privacy_invalidated_at timestamptz"))
    op.execute(text(
        "ALTER TABLE public.batch_runs ADD COLUMN privacy_invalidated_at timestamptz"))
    op.execute(text("""
        CREATE TABLE public.privacy_erasures (
            rule_id uuid PRIMARY KEY,
            rule_row_id uuid NOT NULL
                REFERENCES public.privacy_suppressions(id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL DEFAULT now(),
            first_pass_at timestamptz,
            r2_swept_at timestamptz,
            second_pass_at timestamptz,
            completed_at timestamptz,
            attempts integer NOT NULL DEFAULT 0,
            last_error varchar(256),
            counts jsonb NOT NULL DEFAULT '{}'::jsonb,
            -- Where the current pass stopped: {"store": ..., "after": ...}.
            scan_cursor jsonb NOT NULL DEFAULT '{}'::jsonb,
            CONSTRAINT ck_privacy_erasures_order CHECK (
                (r2_swept_at IS NULL OR first_pass_at IS NOT NULL)
                AND (second_pass_at IS NULL OR r2_swept_at IS NOT NULL)
                AND (completed_at IS NULL OR second_pass_at IS NOT NULL))
        )
    """))
    op.execute(text("ALTER TABLE public.privacy_erasures ENABLE ROW LEVEL SECURITY"))
    op.execute(text("ALTER TABLE public.privacy_erasures FORCE ROW LEVEL SECURITY"))
    op.execute(text("REVOKE ALL ON public.privacy_erasures FROM PUBLIC"))
    # As 129: the rule reader only sees every row because this role owns both privacy
    # tables and RLS is not forced on them. If either drifts it would return NULL for
    # every rule and erase nothing (fail closed, but silently): refuse instead.
    op.execute(text("""
        DO $invariant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_class c
                        WHERE c.oid IN ('public.privacy_suppressions'::regclass,
                                        'public.privacy_requests'::regclass)
                          AND (c.relowner <> (SELECT oid FROM pg_roles
                                               WHERE rolname = current_user)
                               OR c.relforcerowsecurity)) THEN
                RAISE EXCEPTION 'privacy_erasure_rule: the migration role must own the '
                    'privacy tables and RLS must not be forced on them';
            END IF;
        END
        $invariant$
    """))
    op.execute(text(_RULE_FUNCTION_SQL))
    op.execute(text(_FUNCTION_SQL))

    stmts = [f"REVOKE ALL ON public.privacy_erasures FROM {r};" for r in _API_ROLES]
    stmts = [_guarded(r, s) for r, s in zip(_API_ROLES, stmts, strict=True)]
    stmts.append(_guarded(
        "bridgeleads_system",
        "GRANT SELECT, INSERT, UPDATE ON public.privacy_erasures TO bridgeleads_system; "
        "CREATE POLICY privacy_erasures_system ON public.privacy_erasures FOR ALL "
        "TO bridgeleads_system USING (true) WITH CHECK (true);"))
    for tbl, privs in _PURGE_GRANTS:
        stmts.append(f"GRANT {privs} ON public.{tbl} TO bridgeleads_purge;")
    for tbl in _NEW_POLICIES:
        stmts.append(f"DROP POLICY IF EXISTS {tbl}_purge ON public.{tbl};")
        stmts.append(f"CREATE POLICY {tbl}_purge ON public.{tbl} FOR ALL "
                     "TO bridgeleads_purge USING (true) WITH CHECK (true);")
    for fn in (_FN, _RULE_FN):
        stmts.append(f"REVOKE ALL ON FUNCTION public.{fn} FROM PUBLIC;")
        stmts += [_guarded(r, f"REVOKE ALL ON FUNCTION public.{fn} FROM {r};")
                  for r in (*_API_ROLES, "bridgeleads_system")]
    stmts.append(f"GRANT EXECUTE ON FUNCTION public.{_RULE_FN} TO bridgeleads_purge;")
    # Granted before the hand-over: ALTER OWNER carries the old owner's grants across.
    stmts.append(_guarded("bridgeleads_system",
                          f"GRANT EXECUTE ON FUNCTION public.{_FN} TO bridgeleads_system;"))
    body = "\n".join(stmts)
    op.execute(text(f"""
        DO $grants$
        DECLARE
            v_super boolean;
        BEGIN
            {body}
            -- Same temporary hand-over as 112/113: SET on the purge role and CREATE on
            -- public, both taken back in this transaction (a superuser needs neither).
            SELECT rolsuper INTO v_super FROM pg_roles WHERE rolname = current_user;
            IF NOT v_super THEN
                EXECUTE format('GRANT bridgeleads_purge TO %I WITH SET TRUE, INHERIT FALSE',
                               current_user);
            END IF;
            GRANT CREATE ON SCHEMA public TO bridgeleads_purge;
            ALTER FUNCTION public.{_FN} OWNER TO bridgeleads_purge;
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
        $grants$
    """))


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    if op.get_bind().execute(text(
            "SELECT 1 FROM public.results WHERE privacy_erased_at IS NOT NULL "
            "UNION ALL SELECT 1 FROM public.jobs WHERE privacy_invalidated_at IS NOT NULL "
            "UNION ALL SELECT 1 FROM public.batch_runs "
            "WHERE privacy_invalidated_at IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("erased or invalidated rows exist; refusing to downgrade 135")
    # The batch function belongs to the purge role: drop it as that role (as 113).
    op.execute(text(f"""
        DO $drop$
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
            DROP FUNCTION IF EXISTS public.{_FN};
            IF NOT v_super THEN
                RESET ROLE;
                EXECUTE format('REVOKE bridgeleads_purge FROM %I', v_me);
            END IF;
        END
        $drop$
    """))
    op.execute(text(f"DROP FUNCTION IF EXISTS public.{_RULE_FN}"))
    stmts = [f"DROP POLICY IF EXISTS {tbl}_purge ON public.{tbl};" for tbl in _NEW_POLICIES]
    for tbl, privs in _DOWNGRADE_REVOKES:
        stmts.append(f"REVOKE {privs} ON public.{tbl} FROM bridgeleads_purge;")
    body = "\n".join(stmts)
    op.execute(text(f"DO $revokes$ BEGIN\n{body}\nEND $revokes$"))
    op.execute(text("DROP TABLE public.privacy_erasures"))
    op.execute(text("ALTER TABLE public.batch_runs DROP COLUMN privacy_invalidated_at"))
    op.execute(text("ALTER TABLE public.jobs DROP COLUMN privacy_invalidated_at"))
    op.execute(text("ALTER TABLE public.results DROP COLUMN privacy_erased_at"))
