"""Privacy requests from the public web form, and their verification email (137).

Phase 6 P8a (plan tasks/todo-privacy-phase6-p8.md, design v2 after a Codex consult).

A submission IS the received request: the statutory clock starts on receipt, so the
envelope exists from the first moment, unverified. Confirming the emailed link only
records verification.

  privacy_requests.deadline_rules  which deadline rules computed deadline_at (counsel may
                                   change them; old requests keep their own).
  privacy_request_tokens           one row per emailed link: only the SHA-256 of an opaque
                                   random token, its purpose ('verify'), expiry, and when
                                   it was consumed.
  privacy_email_outbox             the durable outbox: written in the submit transaction,
                                   sent by the worker with retries under a durable claim
                                   (claimed_until, a 15-minute lease). 'verification'
                                   carries a link; 'receipt' (an authorized agent's
                                   submission) does not.

Functions (SECURITY DEFINER, owned by the privacy tables' owner like 129, search_path
pinned, nothing personal returned):
  privacy_web_submit()      bridgeleads_app (the public API). Records the request, its
                            encrypted payload, the 'received' event and either
                            'verification_sent' (the requester will get a link) or
                            'in_review' (an agent: the operator checks the signed
                            permission, D1), plus the outbox row. Returns whether it
                            recorded a new request: a repeat of the same kind from the same
                            address within 24 h records nothing (the API answers it
                            identically, and charges it the same quotas as any
                            submission, so no refusal reveals that one exists).
  privacy_web_confirm()     bridgeleads_app. Consumes a token atomically; an opt-out is then
                            'verified' at level 'email_link' (D1), any other kind goes to
                            'in_review' (the operator still matches name and property).
                            Unknown, expired, consumed and closed all do nothing visible.
  privacy_outbox_claim()    bridgeleads_system: claims due rows for 15 minutes (durable:
                            a crashed worker's rows come back after the lease) and returns
                            the ciphertext the worker needs (it holds the decryption key).
  privacy_outbox_token()    bridgeleads_system: stores the hash of the token minted for a
                            claimed verification row, committed BEFORE the email is sent,
                            so every link ever emailed stays valid until it expires.
  privacy_outbox_sent()     bridgeleads_system: marks a claimed row sent.
  privacy_outbox_failed()   bridgeleads_system: backoff, bounded attempts; stores only an
                            error class name, never provider text (it can carry addresses).
  privacy_tokens_purge()    bridgeleads_system: deletes long-expired tokens.
  privacy_requests_due()    bridgeleads_system: open (undecided) requests due within a
                            window, ids, kinds and deadlines only, for the deadline alert.

No role gains a table privilege. Downgrade refuses while any web-form request exists (one
with deadline_rules or an outbox row).

Revision ID: 137
Revises: 136
Create Date: 2026-11-09
"""
from alembic import op
from sqlalchemy import text

revision = "137"
down_revision = "136"
branch_labels = None
depends_on = None

_LOCKED_OUT = ("anon", "authenticated", "service_role", "bridgeleads_app",
               "bridgeleads_system", "bridgeleads_purge")
_APP_FUNCTIONS = (
    "privacy_web_submit(text, text, text, text, boolean, timestamptz, text)",
    "privacy_web_confirm(text)",
)
_WORKER_FUNCTIONS = (
    "privacy_outbox_claim(integer)",
    "privacy_outbox_token(uuid, text, timestamptz)",
    "privacy_outbox_sent(uuid)",
    "privacy_outbox_failed(uuid, text)",
    "privacy_tokens_purge()",
    "privacy_requests_due(interval)",
)
_TABLES = ("privacy_request_tokens", "privacy_email_outbox")

_SQL = r"""
CREATE FUNCTION public.privacy_web_submit(
    p_kind text, p_email_hmac text, p_contact_encrypted text, p_details_encrypted text,
    p_agent boolean, p_deadline_at timestamptz, p_deadline_rules text)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_id uuid;
BEGIN
    IF p_kind NOT IN ('opt_out', 'delete', 'access', 'correct')
       OR p_email_hmac !~ '^[0-9a-f]{64}$'
       OR p_contact_encrypted NOT LIKE 'fe1:%' OR length(p_contact_encrypted) > 4096
       OR (p_details_encrypted IS NOT NULL
           AND (p_details_encrypted NOT LIKE 'fe1:%' OR length(p_details_encrypted) > 16384))
       OR p_agent IS NULL
       OR p_deadline_at IS NULL OR p_deadline_at <= now()
       OR p_deadline_at > now() + interval '120 days'
       OR p_deadline_rules !~ '^[a-z0-9_.-]{1,16}$' THEN
        RAISE EXCEPTION 'privacy_web_submit: invalid input' USING ERRCODE = 'BLD57';
    END IF;
    -- One live request per address and kind a day: a repeat records nothing and sends
    -- nothing (the caller answers it exactly as the first).
    PERFORM pg_advisory_xact_lock(hashtextextended('privacy_web:' || p_email_hmac, 0));
    IF EXISTS (SELECT 1 FROM public.privacy_requests r
                WHERE r.requester_email_hmac = p_email_hmac AND r.kind = p_kind
                  AND r.channel = 'web' AND r.received_at > now() - interval '24 hours') THEN
        RETURN false;
    END IF;
    INSERT INTO public.privacy_requests
        (kind, channel, deadline_at, requester_email_hmac, deadline_rules)
    VALUES (p_kind, 'web', p_deadline_at, p_email_hmac, p_deadline_rules)
    RETURNING id INTO v_id;
    INSERT INTO public.privacy_request_payloads (request_id, contact_encrypted, details_encrypted)
    VALUES (v_id, p_contact_encrypted, p_details_encrypted);
    INSERT INTO public.privacy_request_events (request_id, seq, event_type, actor)
    VALUES (v_id, 1, 'received', 'system'),
           (v_id, 2, CASE WHEN p_agent THEN 'in_review' ELSE 'verification_sent' END, 'system');
    INSERT INTO public.privacy_email_outbox (request_id, kind)
    VALUES (v_id, CASE WHEN p_agent THEN 'receipt' ELSE 'verification' END);
    RETURN true;
END
$fn$;

CREATE FUNCTION public.privacy_web_confirm(p_token_hash text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_request uuid;
    v_kind text;
    v_events text[];
    v_seq integer;
BEGIN
    IF p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$' THEN
        RETURN;
    END IF;
    UPDATE public.privacy_request_tokens t SET consumed_at = now()
     WHERE t.token_hash = p_token_hash AND t.purpose = 'verify'
       AND t.consumed_at IS NULL AND t.expires_at > now()
    RETURNING t.request_id INTO v_request;
    IF v_request IS NULL THEN
        RETURN;
    END IF;
    SELECT r.kind INTO v_kind FROM public.privacy_requests r WHERE r.id = v_request FOR UPDATE;
    SELECT coalesce(array_agg(e.event_type::text ORDER BY e.seq), '{}'), coalesce(max(e.seq), 0)
      INTO v_events, v_seq
      FROM public.privacy_request_events e WHERE e.request_id = v_request;
    IF v_events && ARRAY['decided', 'closed', 'withdrawn', 'verified', 'in_review'] THEN
        RETURN;
    END IF;
    IF v_kind = 'opt_out' THEN
        INSERT INTO public.privacy_request_events
            (request_id, seq, event_type, verification_level, actor)
        VALUES (v_request, v_seq + 1, 'verified', 'email_link', 'system');
    ELSE
        INSERT INTO public.privacy_request_events (request_id, seq, event_type, actor)
        VALUES (v_request, v_seq + 1, 'in_review', 'system');
    END IF;
END
$fn$;

CREATE FUNCTION public.privacy_outbox_claim(p_limit integer)
RETURNS TABLE (outbox_id uuid, email_kind text, request_kind text, received_at timestamptz,
               deadline_at timestamptz, contact_encrypted text, attempts integer)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    WITH picked AS (
        SELECT o.id FROM public.privacy_email_outbox o
         WHERE o.sent_at IS NULL AND o.gave_up_at IS NULL AND o.next_attempt_at <= now()
           AND (o.claimed_until IS NULL OR o.claimed_until < now())
         ORDER BY o.next_attempt_at, o.id
         LIMIT least(greatest(p_limit, 1), 100)
           FOR UPDATE SKIP LOCKED),
    claimed AS (
        UPDATE public.privacy_email_outbox o SET claimed_until = now() + interval '15 minutes'
          FROM picked WHERE o.id = picked.id
        RETURNING o.id, o.kind, o.request_id, o.attempts)
    SELECT c.id, c.kind::text, r.kind::text, r.received_at, r.deadline_at,
           p.contact_encrypted, c.attempts
      FROM claimed c
      JOIN public.privacy_requests r ON r.id = c.request_id
      JOIN public.privacy_request_payloads p ON p.request_id = r.id
$fn$;

CREATE FUNCTION public.privacy_outbox_token(p_outbox uuid, p_token_hash text,
                                            p_expires_at timestamptz)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_request uuid;
BEGIN
    SELECT o.request_id INTO v_request FROM public.privacy_email_outbox o
     WHERE o.id = p_outbox AND o.kind = 'verification' AND o.sent_at IS NULL
       AND o.claimed_until > now();
    IF v_request IS NULL THEN
        RAISE EXCEPTION 'privacy_outbox_token: not a claimed verification row'
            USING ERRCODE = 'BLD58';
    END IF;
    IF p_token_hash IS NULL OR p_token_hash !~ '^[0-9a-f]{64}$'
       OR p_expires_at IS NULL OR p_expires_at <= now()
       OR p_expires_at > now() + interval '30 days' THEN
        RAISE EXCEPTION 'privacy_outbox_token: invalid token' USING ERRCODE = 'BLD58';
    END IF;
    INSERT INTO public.privacy_request_tokens (request_id, purpose, token_hash, expires_at)
    VALUES (v_request, 'verify', p_token_hash, p_expires_at);
END
$fn$;

CREATE FUNCTION public.privacy_outbox_sent(p_outbox uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_request uuid;
    v_kind text;
BEGIN
    UPDATE public.privacy_email_outbox o
       SET sent_at = now(), claimed_until = NULL, last_error = NULL
     WHERE o.id = p_outbox AND o.sent_at IS NULL AND o.claimed_until > now()
    RETURNING o.request_id, o.kind INTO v_request, v_kind;
    IF v_request IS NULL THEN
        RAISE EXCEPTION 'privacy_outbox_sent: not a claimed unsent row' USING ERRCODE = 'BLD58';
    END IF;
    IF v_kind = 'verification' AND NOT EXISTS (
            SELECT 1 FROM public.privacy_request_tokens t
             WHERE t.request_id = v_request AND t.purpose = 'verify') THEN
        RAISE EXCEPTION 'privacy_outbox_sent: a verification email needs its token'
            USING ERRCODE = 'BLD58';
    END IF;
END
$fn$;

CREATE FUNCTION public.privacy_outbox_failed(p_outbox uuid, p_error text) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    UPDATE public.privacy_email_outbox o
       SET attempts = o.attempts + 1,
           -- An error CLASS only: provider messages can carry the recipient's address.
           last_error = CASE WHEN p_error ~ '^[A-Za-z][A-Za-z0-9_.]{0,63}$' THEN p_error
                             ELSE 'error' END,
           claimed_until = NULL,
           next_attempt_at = now() + least(interval '1 minute' * power(2, o.attempts),
                                           interval '6 hours'),
           gave_up_at = CASE WHEN o.attempts + 1 >= 10 THEN now() END
     -- Only the holder of the live claim: a worker whose lease lapsed must not touch
     -- the row another worker has claimed since.
     WHERE o.id = p_outbox AND o.sent_at IS NULL AND o.claimed_until > now()
$fn$;

CREATE FUNCTION public.privacy_requests_due(p_within interval)
RETURNS TABLE (request_id uuid, kind text, deadline_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    SELECT r.id, r.kind::text, coalesce(
               (SELECT max(e.deadline_at) FROM public.privacy_request_events e
                 WHERE e.request_id = r.id AND e.event_type = 'extended'), r.deadline_at)
      FROM public.privacy_requests r
     WHERE NOT EXISTS (SELECT 1 FROM public.privacy_request_events e
                        WHERE e.request_id = r.id
                          AND e.event_type IN ('decided', 'closed', 'withdrawn'))
       AND coalesce((SELECT max(e.deadline_at) FROM public.privacy_request_events e
                      WHERE e.request_id = r.id AND e.event_type = 'extended'),
                    r.deadline_at) <= now() + least(p_within, interval '60 days')
     ORDER BY 3, 1
$fn$;

CREATE FUNCTION public.privacy_tokens_purge() RETURNS integer
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    WITH gone AS (DELETE FROM public.privacy_request_tokens t
                   WHERE t.expires_at < now() - interval '30 days' RETURNING 1)
    SELECT count(*)::integer FROM gone
$fn$;
"""


def _guarded(role: str, stmt: str) -> str:
    return (f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN {stmt} "
            "END IF;")


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    # As 129/135: the definer functions read every row only because this role owns the
    # privacy tables and RLS is not forced on them. Refuse rather than ship functions
    # that would silently see nothing.
    op.execute(text("""
        DO $invariant$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_class c
                        WHERE c.oid IN ('public.privacy_requests'::regclass,
                                        'public.privacy_request_payloads'::regclass,
                                        'public.privacy_request_events'::regclass)
                          AND (c.relowner <> (SELECT oid FROM pg_roles
                                               WHERE rolname = current_user)
                               OR c.relforcerowsecurity)) THEN
                RAISE EXCEPTION 'privacy web intake: the migration role must own the '
                    'privacy tables and RLS must not be forced on them';
            END IF;
        END
        $invariant$
    """))
    op.execute(text("ALTER TABLE public.privacy_requests ADD COLUMN deadline_rules varchar(16)"))
    op.execute(text("""
        CREATE TABLE public.privacy_request_tokens (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            request_id uuid NOT NULL REFERENCES public.privacy_requests(id) ON DELETE CASCADE,
            purpose varchar(16) NOT NULL DEFAULT 'verify',
            token_hash char(64) NOT NULL UNIQUE,
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            consumed_at timestamptz,
            CONSTRAINT ck_privacy_request_tokens_hash CHECK (token_hash ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_privacy_request_tokens_purpose CHECK (purpose IN ('verify')),
            CONSTRAINT ck_privacy_request_tokens_expiry CHECK (expires_at > created_at)
        )
    """))
    op.execute(text("""
        CREATE TABLE public.privacy_email_outbox (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            request_id uuid NOT NULL REFERENCES public.privacy_requests(id) ON DELETE CASCADE,
            kind varchar(16) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            attempts integer NOT NULL DEFAULT 0,
            next_attempt_at timestamptz NOT NULL DEFAULT now(),
            claimed_until timestamptz,
            sent_at timestamptz,
            gave_up_at timestamptz,
            last_error varchar(256),
            CONSTRAINT uq_privacy_email_outbox UNIQUE (request_id, kind),
            CONSTRAINT ck_privacy_email_outbox_kind CHECK (kind IN ('verification', 'receipt'))
        )
    """))
    op.execute(text("CREATE INDEX ix_privacy_email_outbox_due ON public.privacy_email_outbox "
                    "(next_attempt_at) WHERE sent_at IS NULL AND gave_up_at IS NULL"))
    op.execute(text("CREATE INDEX ix_privacy_requests_web_dedupe ON public.privacy_requests "
                    "(requester_email_hmac, kind, received_at) WHERE channel = 'web'"))
    for table in _TABLES:
        op.execute(text(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY"))
        op.execute(text(f"REVOKE ALL ON public.{table} FROM PUBLIC"))
    op.execute(text(_SQL))

    stmts = [_guarded(r, f"REVOKE ALL ON public.{t} FROM {r};")
             for t in _TABLES for r in _LOCKED_OUT]
    for fn in _APP_FUNCTIONS + _WORKER_FUNCTIONS:
        stmts.append(f"REVOKE ALL ON FUNCTION public.{fn} FROM PUBLIC;")
        stmts += [_guarded(r, f"REVOKE ALL ON FUNCTION public.{fn} FROM {r};")
                  for r in _LOCKED_OUT]
    stmts += [_guarded("bridgeleads_app",
                       f"GRANT EXECUTE ON FUNCTION public.{fn} TO bridgeleads_app;")
              for fn in _APP_FUNCTIONS]
    stmts += [_guarded("bridgeleads_system",
                       f"GRANT EXECUTE ON FUNCTION public.{fn} TO bridgeleads_system;")
              for fn in _WORKER_FUNCTIONS]
    op.execute(text("DO $privacy_137$\nBEGIN\n" + "\n".join(stmts) + "\nEND\n$privacy_137$;"))


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute(text("""
        DO $refuse$
        BEGIN
            -- Only the web form writes deadline_rules and outbox rows ('web' alone does
            -- not identify it: an operator may record a request as 'web' too).
            IF EXISTS (SELECT 1 FROM public.privacy_requests WHERE deadline_rules IS NOT NULL)
               OR EXISTS (SELECT 1 FROM public.privacy_email_outbox) THEN
                RAISE EXCEPTION 'web-form privacy requests exist; refusing to drop their '
                    'outbox, tokens and deadline rules';
            END IF;
        END
        $refuse$
    """))
    for fn in _APP_FUNCTIONS + _WORKER_FUNCTIONS:
        op.execute(text(f"DROP FUNCTION IF EXISTS public.{fn}"))
    op.execute(text("DROP TABLE public.privacy_email_outbox"))
    op.execute(text("DROP TABLE public.privacy_request_tokens"))
    op.execute(text("DROP INDEX IF EXISTS public.ix_privacy_requests_web_dedupe"))
    op.execute(text("ALTER TABLE public.privacy_requests DROP COLUMN deadline_rules"))
