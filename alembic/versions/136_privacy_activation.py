"""Privacy activation: the write side of the suppression rules, and the guard lifted (136).

Phase 6 P7 (plan tasks/todo-privacy-phase6-p7.md, design sections 8 and 10). Until now no
role could write a privacy row and migration 125's CHECK admitted state 'pending' only.

  ck_privacy_suppressions_activation_guard   REPLACED by
  ck_privacy_suppressions_state              state IN ('pending', 'active', 'lifted')
  ck_privacy_suppressions_enforceable        a non-pending row's anchor is property, phone
                                             or email: the rule loader cannot compute a
                                             mailing anchor and would refuse EVERY rule
                                             (fail closed, globally) if one went active.
  privacy_suppressions.actor_user_id         the operator behind the row. Nullable: only
                                             the functions below write rule rows, and each
                                             sets it.
  privacy_review_releases                    append-only: an operator decided that a REVIEW
                                             match on this result is not the rule's subject.
                                             authorize() honours it for REVIEW only; an exact
                                             (WITHHELD) match is never releasable.
  privacy_review_release_rows()              129-style definer reader for the worker role.

Operator functions (SECURITY INVOKER, owned by the migration role, EXECUTE revoked from
PUBLIC and every runtime and API role: only the migration role, i.e. an operator with
Railway access running scripts/privacy_admin.py, can call them). Each takes the operator's
user id and refuses unless it is an admin with MFA enabled. Each locks the request row
first, so every write to one request is serialised.

  privacy_record_request   envelope + encrypted payload + 'received' event.
  privacy_record_event     lifecycle-checked audit event (seq = max + 1 under the lock).
  privacy_add_rule         a PENDING rule row; only opt-out and deletion requests, verified,
                           not yet decided; a further key-version row joins an existing
                           pending rule of the same request and kinds.
  privacy_activate_rule    every pending row of the rule becomes active at once; a deletion
                           request also gets its privacy_erasures row (P6's driver).
  privacy_lift_rule        opt-out only (erased data cannot come back): lifted rows, then the
                           rule's results unflagged for re-decision, King tombstones dropped,
                           NTS notices released. Returns the result ids.
  privacy_release_review   records a release and unflags that one result for re-decision.

What the database does NOT check: that the key and normalisation versions are live. The CLI
commits an activation, lift or release only if the rule snapshot loads afterwards in the
same transaction (src/privacy/authorize.py), so a rule the loader rejects never commits.

The application keys (src/privacy/activation.py IMPLEMENTED_ENFORCEMENT_VERSION and the
PRIVACY_ENFORCEMENT_VERSION setting) still gate activation in the CLI.

Downgrade refuses while any non-pending rule row or release exists.

Revision ID: 136
Revises: 135
Create Date: 2026-11-08
"""
from alembic import op
from sqlalchemy import text

revision = "136"
down_revision = "135"
branch_labels = None
depends_on = None

_RUNTIME_READER = "bridgeleads_system"
_LOCKED_OUT = ("anon", "authenticated", "service_role", "bridgeleads_app",
               "bridgeleads_system", "bridgeleads_purge")
_OPERATOR_FUNCTIONS = (
    "privacy_record_request(text, text, timestamptz, text, text, text, uuid)",
    "privacy_record_event(uuid, text, text, text, timestamptz, uuid)",
    "privacy_add_rule(uuid, text, text, text, text, text, text, uuid, uuid)",
    "privacy_activate_rule(uuid, uuid)",
    "privacy_lift_rule(uuid, uuid)",
    "privacy_release_review(uuid, uuid, uuid)",
)
_READER = "privacy_review_release_rows()"

_SQL = r"""
CREATE FUNCTION public.privacy_operator_check(p_actor uuid) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    IF p_actor IS NULL OR NOT EXISTS (
            SELECT 1 FROM public.users u
             WHERE u.id = p_actor AND u.is_admin AND u.mfa_enabled
               AND u.deletion_state IS NULL) THEN
        RAISE EXCEPTION 'privacy: the operator must be an admin with MFA enabled'
            USING ERRCODE = 'BLD50';
    END IF;
END
$fn$;

-- The request row, locked, and its event types in order. Every operator write starts here.
CREATE FUNCTION public.privacy_lock_request(p_request uuid, OUT kind text, OUT events text[])
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    SELECT r.kind INTO kind FROM public.privacy_requests r WHERE r.id = p_request FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'privacy: no such request' USING ERRCODE = 'BLD51';
    END IF;
    SELECT coalesce(array_agg(e.event_type::text ORDER BY e.seq), '{}') INTO events
      FROM public.privacy_request_events e WHERE e.request_id = p_request;
END
$fn$;

CREATE FUNCTION public.privacy_record_request(
    p_kind text, p_channel text, p_deadline_at timestamptz, p_requester_email_hmac text,
    p_contact_encrypted text, p_details_encrypted text, p_actor uuid)
RETURNS uuid
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_id uuid;
BEGIN
    PERFORM public.privacy_operator_check(p_actor);
    INSERT INTO public.privacy_requests (kind, channel, deadline_at, requester_email_hmac)
    VALUES (p_kind, p_channel, p_deadline_at, p_requester_email_hmac)
    RETURNING id INTO v_id;
    INSERT INTO public.privacy_request_payloads (request_id, contact_encrypted, details_encrypted)
    VALUES (v_id, p_contact_encrypted, p_details_encrypted);
    INSERT INTO public.privacy_request_events (request_id, seq, event_type, actor, actor_user_id)
    VALUES (v_id, 1, 'received', 'operator', p_actor);
    RETURN v_id;
END
$fn$;

CREATE FUNCTION public.privacy_record_event(
    p_request uuid, p_event_type text, p_verification_level text, p_decision_code text,
    p_deadline_at timestamptz, p_actor uuid)
RETURNS integer
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_req record;
    v_seq integer;
BEGIN
    PERFORM public.privacy_operator_check(p_actor);
    v_req := public.privacy_lock_request(p_request);
    IF v_req.events && ARRAY['closed', 'withdrawn'] THEN
        RAISE EXCEPTION 'privacy: the request is closed' USING ERRCODE = 'BLD52';
    ELSIF p_event_type = 'received' THEN
        RAISE EXCEPTION 'privacy: received is recorded at intake only' USING ERRCODE = 'BLD52';
    ELSIF p_event_type IN ('verified', 'decided') AND p_event_type = ANY(v_req.events) THEN
        RAISE EXCEPTION 'privacy: the request is already %', p_event_type
            USING ERRCODE = 'BLD52';
    ELSIF p_event_type = 'closed' AND NOT ('decided' = ANY(v_req.events)) THEN
        RAISE EXCEPTION 'privacy: a request is closed only after a decision'
            USING ERRCODE = 'BLD52';
    END IF;
    SELECT coalesce(max(e.seq), 0) + 1 INTO v_seq
      FROM public.privacy_request_events e WHERE e.request_id = p_request;
    INSERT INTO public.privacy_request_events
        (request_id, seq, event_type, verification_level, decision_code, deadline_at,
         actor, actor_user_id)
    VALUES (p_request, v_seq, p_event_type, p_verification_level, p_decision_code,
            p_deadline_at, 'operator', p_actor);
    RETURN v_seq;
END
$fn$;

-- A request may still change its rules while it is verified and undecided.
CREATE FUNCTION public.privacy_assert_open_for_rules(p_kind text, p_events text[])
RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $fn$
BEGIN
    IF p_kind NOT IN ('opt_out', 'delete') THEN
        RAISE EXCEPTION 'privacy: only opt-out and deletion requests make rules'
            USING ERRCODE = 'BLD53';
    ELSIF NOT ('verified' = ANY(p_events)) THEN
        RAISE EXCEPTION 'privacy: the request is not verified' USING ERRCODE = 'BLD53';
    ELSIF p_events && ARRAY['decided', 'closed', 'withdrawn'] THEN
        RAISE EXCEPTION 'privacy: the request is already decided' USING ERRCODE = 'BLD53';
    END IF;
END
$fn$;

CREATE FUNCTION public.privacy_add_rule(
    p_request uuid, p_anchor_kind text, p_anchor_hmac text, p_subject_kind text,
    p_subject_hmac text, p_key_version text, p_normalization_version text, p_actor uuid,
    p_rule_id uuid DEFAULT NULL)
RETURNS uuid
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_req record;
    v_rule uuid := coalesce(p_rule_id, gen_random_uuid());
BEGIN
    PERFORM public.privacy_operator_check(p_actor);
    v_req := public.privacy_lock_request(p_request);
    PERFORM public.privacy_assert_open_for_rules(v_req.kind, v_req.events);
    IF p_anchor_kind NOT IN ('property', 'phone', 'email') THEN
        RAISE EXCEPTION 'privacy: a % anchor cannot be enforced', p_anchor_kind
            USING ERRCODE = 'BLD53';
    END IF;
    IF p_rule_id IS NOT NULL THEN
        -- A further key-version row of a rule this request already holds.
        IF NOT EXISTS (SELECT 1 FROM public.privacy_suppressions s
                        WHERE s.rule_id = p_rule_id AND s.request_id = p_request
                          AND s.anchor_kind = p_anchor_kind
                          AND s.subject_kind = p_subject_kind) THEN
            RAISE EXCEPTION 'privacy: no such rule on this request' USING ERRCODE = 'BLD53';
        END IF;
        IF EXISTS (SELECT 1 FROM public.privacy_suppressions s
                    WHERE s.rule_id = p_rule_id AND s.state <> 'pending') THEN
            RAISE EXCEPTION 'privacy: the rule is no longer pending' USING ERRCODE = 'BLD53';
        END IF;
    END IF;
    INSERT INTO public.privacy_suppressions
        (rule_id, request_id, anchor_kind, anchor_hmac, subject_kind, subject_hmac, scope,
         key_version, normalization_version, state, actor_user_id)
    VALUES (v_rule, p_request, p_anchor_kind, p_anchor_hmac, p_subject_kind, p_subject_hmac,
            'subject', p_key_version, p_normalization_version, 'pending', p_actor);
    RETURN v_rule;
END
$fn$;

-- The request behind a rule, locked; NULL request when the rule does not exist.
CREATE FUNCTION public.privacy_lock_rule(p_rule uuid, OUT request_id uuid, OUT kind text,
                                         OUT events text[])
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_req record;
BEGIN
    SELECT s.request_id INTO request_id FROM public.privacy_suppressions s
     WHERE s.rule_id = p_rule LIMIT 1;
    IF request_id IS NULL THEN
        RAISE EXCEPTION 'privacy: no such rule' USING ERRCODE = 'BLD54';
    END IF;
    v_req := public.privacy_lock_request(request_id);
    kind := v_req.kind;
    events := v_req.events;
END
$fn$;

CREATE FUNCTION public.privacy_activate_rule(p_rule uuid, p_actor uuid) RETURNS integer
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_req record;
    v_ids uuid[];
BEGIN
    PERFORM public.privacy_operator_check(p_actor);
    v_req := public.privacy_lock_rule(p_rule);
    PERFORM public.privacy_assert_open_for_rules(v_req.kind, v_req.events);
    IF EXISTS (SELECT 1 FROM public.privacy_suppressions s
                WHERE s.rule_id = p_rule AND s.state <> 'pending') THEN
        RAISE EXCEPTION 'privacy: the rule is already active or lifted'
            USING ERRCODE = 'BLD54';
    END IF;
    WITH ins AS (
        INSERT INTO public.privacy_suppressions
            (rule_id, request_id, anchor_kind, anchor_hmac, subject_kind, subject_hmac, scope,
             key_version, normalization_version, state, actor_user_id)
        SELECT s.rule_id, s.request_id, s.anchor_kind, s.anchor_hmac, s.subject_kind,
               s.subject_hmac, s.scope, s.key_version, s.normalization_version, 'active',
               p_actor
          FROM public.privacy_suppressions s
         WHERE s.rule_id = p_rule AND s.state = 'pending'
        RETURNING id, key_version)
    SELECT array_agg(ins.id ORDER BY ins.key_version) INTO v_ids FROM ins;
    IF v_req.kind = 'delete' THEN
        INSERT INTO public.privacy_erasures (rule_id, rule_row_id)
        VALUES (p_rule, v_ids[1]) ON CONFLICT (rule_id) DO NOTHING;
    END IF;
    RETURN cardinality(v_ids);
END
$fn$;

CREATE FUNCTION public.privacy_lift_rule(p_rule uuid, p_actor uuid) RETURNS SETOF uuid
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_req record;
    v_ids uuid[];
BEGIN
    PERFORM public.privacy_operator_check(p_actor);
    v_req := public.privacy_lock_rule(p_rule);
    IF v_req.kind <> 'opt_out' THEN
        RAISE EXCEPTION 'privacy: only an opt-out is lifted; erased data cannot come back'
            USING ERRCODE = 'BLD55';
    END IF;
    IF EXISTS (SELECT 1 FROM public.privacy_suppressions s
                WHERE s.rule_id = p_rule AND s.state = 'lifted') THEN
        RAISE EXCEPTION 'privacy: the rule is already lifted' USING ERRCODE = 'BLD55';
    END IF;
    SELECT array_agg(s.id) INTO v_ids FROM public.privacy_suppressions s
     WHERE s.rule_id = p_rule AND s.state = 'active';
    IF v_ids IS NULL THEN
        RAISE EXCEPTION 'privacy: the rule is not active' USING ERRCODE = 'BLD55';
    END IF;
    INSERT INTO public.privacy_suppressions
        (rule_id, request_id, anchor_kind, anchor_hmac, subject_kind, subject_hmac, scope,
         key_version, normalization_version, state, actor_user_id)
    SELECT s.rule_id, s.request_id, s.anchor_kind, s.anchor_hmac, s.subject_kind,
           s.subject_hmac, s.scope, s.key_version, s.normalization_version, 'lifted', p_actor
      FROM public.privacy_suppressions s WHERE s.id = ANY(v_ids);
    DELETE FROM public.king_parcel_owners k WHERE k.privacy_suppression_id = ANY(v_ids);
    UPDATE public.nts_notices n SET privacy_suppression_id = NULL
     WHERE n.privacy_suppression_id = ANY(v_ids);
    -- The caller re-decides these rows in the same transaction (another rule may match).
    -- Rows of a purging or deleted account belong to the purge (113) and stay as they are.
    RETURN QUERY
        UPDATE public.results r
           SET privacy_suppressed_at = NULL, privacy_suppression_id = NULL,
               privacy_checked_gen = 0
          FROM public.users u
         WHERE u.id = r.user_id AND r.privacy_suppression_id = ANY(v_ids)
           AND r.privacy_erased_at IS NULL
           AND (u.deletion_state IS NULL OR u.deletion_state NOT IN ('purging', 'deleted'))
        RETURNING r.id;
END
$fn$;

CREATE FUNCTION public.privacy_release_review(p_result uuid, p_rule uuid, p_actor uuid)
RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_req record;
BEGIN
    PERFORM public.privacy_operator_check(p_actor);
    v_req := public.privacy_lock_rule(p_rule);
    -- Only a row flagged by this rule, which is still active and has erased nothing there.
    PERFORM 1 FROM public.results r
      JOIN public.privacy_suppressions s ON s.id = r.privacy_suppression_id
     WHERE r.id = p_result AND s.rule_id = p_rule AND s.state = 'active'
       AND r.privacy_erased_at IS NULL
       AND NOT EXISTS (SELECT 1 FROM public.privacy_suppressions l
                        WHERE l.rule_id = p_rule AND l.state = 'lifted')
       FOR UPDATE OF r;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'privacy: the result is not held by this active rule'
            USING ERRCODE = 'BLD56';
    END IF;
    INSERT INTO public.privacy_review_releases (rule_id, result_id, actor_user_id)
    VALUES (p_rule, p_result, p_actor);
    -- Re-decided by the caller in the same transaction: an exact match is flagged again.
    UPDATE public.results SET privacy_suppressed_at = NULL, privacy_suppression_id = NULL,
                              privacy_checked_gen = 0
     WHERE id = p_result;
END
$fn$;

CREATE FUNCTION public.privacy_review_release_rows()
RETURNS TABLE (rule_id text, result_id text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    SELECT rr.rule_id::text, rr.result_id::text FROM public.privacy_review_releases rr
$fn$;
"""

_HELPERS = (
    "privacy_operator_check(uuid)",
    "privacy_lock_request(uuid)",
    "privacy_assert_open_for_rules(text, text[])",
    "privacy_lock_rule(uuid)",
)


def _guarded(role: str, stmt: str) -> str:
    return (f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN {stmt} "
            "END IF;")


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute(text("ALTER TABLE public.privacy_suppressions "
                    "DROP CONSTRAINT ck_privacy_suppressions_activation_guard"))
    op.execute(text("ALTER TABLE public.privacy_suppressions ADD CONSTRAINT "
                    "ck_privacy_suppressions_state "
                    "CHECK (state IN ('pending', 'active', 'lifted'))"))
    op.execute(text("ALTER TABLE public.privacy_suppressions ADD CONSTRAINT "
                    "ck_privacy_suppressions_enforceable "
                    "CHECK (state = 'pending' OR anchor_kind IN ('property', 'phone', 'email'))"))
    op.execute(text("ALTER TABLE public.privacy_suppressions ADD COLUMN actor_user_id uuid"))
    op.execute(text("""
        CREATE TABLE public.privacy_review_releases (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            rule_id uuid NOT NULL,
            result_id uuid NOT NULL REFERENCES public.results(id) ON DELETE CASCADE,
            actor_user_id uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_privacy_review_releases UNIQUE (rule_id, result_id)
        )
    """))
    op.execute(text(
        "CREATE TRIGGER privacy_review_releases_no_update BEFORE UPDATE "
        "ON public.privacy_review_releases "
        "FOR EACH ROW EXECUTE FUNCTION public.privacy_rows_immutable()"))
    op.execute(text("ALTER TABLE public.privacy_review_releases "
                    "ENABLE ALWAYS TRIGGER privacy_review_releases_no_update"))
    op.execute(text("ALTER TABLE public.privacy_review_releases ENABLE ROW LEVEL SECURITY"))
    op.execute(text("REVOKE ALL ON public.privacy_review_releases FROM PUBLIC"))
    op.execute(text(_SQL))

    stmts = [_guarded(r, f"REVOKE ALL ON public.privacy_review_releases FROM {r};")
             for r in _LOCKED_OUT]
    for fn in _OPERATOR_FUNCTIONS + _HELPERS + (_READER,):
        stmts.append(f"REVOKE ALL ON FUNCTION public.{fn} FROM PUBLIC;")
        stmts += [_guarded(r, f"REVOKE ALL ON FUNCTION public.{fn} FROM {r};")
                  for r in _LOCKED_OUT]
    stmts.append(_guarded(_RUNTIME_READER,
                          f"GRANT EXECUTE ON FUNCTION public.{_READER} TO {_RUNTIME_READER};"))
    op.execute(text("DO $privacy_136$\nBEGIN\n" + "\n".join(stmts) + "\nEND\n$privacy_136$;"))


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute(text("""
        DO $refuse$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.privacy_suppressions WHERE state <> 'pending')
               OR EXISTS (SELECT 1 FROM public.privacy_review_releases) THEN
                RAISE EXCEPTION 'privacy rules are active, lifted or released; refusing to '
                    'restore the pending-only guard';
            END IF;
        END
        $refuse$
    """))
    for fn in (_READER,) + _OPERATOR_FUNCTIONS + _HELPERS:
        op.execute(text(f"DROP FUNCTION IF EXISTS public.{fn}"))
    op.execute(text("DROP TABLE public.privacy_review_releases"))
    op.execute(text("ALTER TABLE public.privacy_suppressions DROP COLUMN actor_user_id"))
    op.execute(text("ALTER TABLE public.privacy_suppressions "
                    "DROP CONSTRAINT ck_privacy_suppressions_enforceable"))
    op.execute(text("ALTER TABLE public.privacy_suppressions "
                    "DROP CONSTRAINT ck_privacy_suppressions_state"))
    op.execute(text("ALTER TABLE public.privacy_suppressions ADD CONSTRAINT "
                    "ck_privacy_suppressions_activation_guard CHECK (state = 'pending')"))
