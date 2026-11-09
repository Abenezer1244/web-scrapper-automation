"""Privacy-request retention: the 24-month purge, and legal holds that stop it (138).

Design section 11, D5 (owner default, pending counsel): request records are kept 24 months;
the requester's contact is deleted 24 months after the request closes; suppression rules
are kept indefinitely. Plan and Codex consult: tasks/todo-compliance-eng-2026-11-09.md.

  privacy_legal_holds        append-only: an operator places or releases a hold on a request
                             (litigation, a regulator, counsel). The latest action wins.
  privacy_hold_request()     owner-only (the operator CLI, admin + MFA checked), like 136.
  privacy_retention_purge()  bridgeleads_system (a daily beat). A request is due when its
                             `closed` or `withdrawn` event (136's lifecycle allows nothing
                             after either) is at least 24 months old, and
                               - no outbox email of it is unsent or claimed,
                               - its latest hold action is not `hold`,
                               - every erasure of its rules has completed.
                             Then:
                               - no suppression rule references it: the request is deleted,
                                 with its payload, events, tokens, outbox and holds (CASCADE);
                               - a rule references it (125's FK RESTRICT: the paperwork lives
                                 as long as the rule): only its payload (the requester's
                                 contact and details) is deleted. Kept: kind, channel, received
                                 and deadline times, deadline rules, the email's keyed blind
                                 index, the events (no personal data) and the rule.
                             The window is fixed here: changing it is a migration, with counsel.
                             Bounded per call; returns counts only.

No role gains a table privilege. Downgrade refuses while any hold exists.

Revision ID: 138
Revises: 137
Create Date: 2026-11-09
"""
from alembic import op
from sqlalchemy import text

revision = "138"
down_revision = "137"
branch_labels = None
depends_on = None

_LOCKED_OUT = ("anon", "authenticated", "service_role", "bridgeleads_app",
               "bridgeleads_system", "bridgeleads_purge")
_PURGE = "privacy_retention_purge(integer)"
_HOLD = "privacy_hold_request(uuid, text, uuid)"

_SQL = r"""
CREATE FUNCTION public.privacy_hold_request(p_request uuid, p_action text, p_actor uuid)
RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_req record;
BEGIN
    PERFORM public.privacy_operator_check(p_actor);
    v_req := public.privacy_lock_request(p_request);
    IF p_action NOT IN ('hold', 'release') THEN
        RAISE EXCEPTION 'privacy: a hold is placed or released' USING ERRCODE = 'BLD59';
    END IF;
    INSERT INTO public.privacy_legal_holds (request_id, action, actor_user_id)
    VALUES (p_request, p_action, p_actor);
END
$fn$;

CREATE FUNCTION public.privacy_retention_purge(p_limit integer)
RETURNS TABLE (requests_deleted integer, payloads_deleted integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_req record;
    v_requests integer := 0;
    v_payloads integer := 0;
    v_n integer;
BEGIN
    FOR v_req IN
        SELECT r.id,
               EXISTS (SELECT 1 FROM public.privacy_suppressions s
                        WHERE s.request_id = r.id) AS has_rule
          FROM public.privacy_requests r
         -- terminal_at: the request's ONE closed/withdrawn event. Malformed data with
         -- two yields NULL here and the request is skipped (fail closed).
         WHERE (SELECT min(e.occurred_at) FROM public.privacy_request_events e
                 WHERE e.request_id = r.id AND e.event_type IN ('closed', 'withdrawn')
                HAVING count(*) = 1)
               <= now() - interval '24 months'
           AND NOT EXISTS (SELECT 1 FROM public.privacy_email_outbox o
                            WHERE o.request_id = r.id
                              AND ((o.sent_at IS NULL AND o.gave_up_at IS NULL)
                                   OR o.claimed_until > now()))
           AND coalesce((SELECT h.action FROM public.privacy_legal_holds h
                          WHERE h.request_id = r.id
                          ORDER BY h.created_at DESC, h.id DESC LIMIT 1), 'release') <> 'hold'
           AND NOT EXISTS (SELECT 1 FROM public.privacy_suppressions s
                             JOIN public.privacy_erasures x ON x.rule_id = s.rule_id
                            WHERE s.request_id = r.id AND x.completed_at IS NULL)
           -- Something to do: a rule-held request whose payload is already gone is done.
           AND (EXISTS (SELECT 1 FROM public.privacy_request_payloads p
                         WHERE p.request_id = r.id)
                OR NOT EXISTS (SELECT 1 FROM public.privacy_suppressions s
                                WHERE s.request_id = r.id))
         ORDER BY r.received_at, r.id
         LIMIT least(greatest(coalesce(p_limit, 1), 1), 500)
           FOR UPDATE OF r SKIP LOCKED
    LOOP
        -- The outbox can change between selection and here (a worker claims a row):
        -- lock the request's rows and look again before deleting anything. A claim in
        -- progress makes this wait; a committed claim or an unsent email skips it.
        PERFORM 1 FROM public.privacy_email_outbox o WHERE o.request_id = v_req.id
          FOR UPDATE;
        IF EXISTS (SELECT 1 FROM public.privacy_email_outbox o
                    WHERE o.request_id = v_req.id
                      AND ((o.sent_at IS NULL AND o.gave_up_at IS NULL)
                           OR o.claimed_until > now())) THEN
            CONTINUE;
        END IF;
        DELETE FROM public.privacy_request_payloads p WHERE p.request_id = v_req.id;
        GET DIAGNOSTICS v_n = ROW_COUNT;
        v_payloads := v_payloads + v_n;
        IF NOT v_req.has_rule THEN
            DELETE FROM public.privacy_requests r WHERE r.id = v_req.id;
            v_requests := v_requests + 1;
        END IF;
    END LOOP;
    RETURN QUERY SELECT v_requests, v_payloads;
END
$fn$;
"""


def _guarded(role: str, stmt: str) -> str:
    return (f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN {stmt} "
            "END IF;")


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute(text("""
        CREATE TABLE public.privacy_legal_holds (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            request_id uuid NOT NULL REFERENCES public.privacy_requests(id) ON DELETE CASCADE,
            action varchar(8) NOT NULL,
            actor_user_id uuid NOT NULL,
            created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT ck_privacy_legal_holds_action CHECK (action IN ('hold', 'release'))
        )
    """))
    op.execute(text("CREATE INDEX ix_privacy_legal_holds_request "
                    "ON public.privacy_legal_holds (request_id, created_at DESC)"))
    op.execute(text("CREATE TRIGGER privacy_legal_holds_no_update BEFORE UPDATE "
                    "ON public.privacy_legal_holds "
                    "FOR EACH ROW EXECUTE FUNCTION public.privacy_rows_immutable()"))
    op.execute(text("ALTER TABLE public.privacy_legal_holds "
                    "ENABLE ALWAYS TRIGGER privacy_legal_holds_no_update"))
    op.execute(text("ALTER TABLE public.privacy_legal_holds ENABLE ROW LEVEL SECURITY"))
    op.execute(text("REVOKE ALL ON public.privacy_legal_holds FROM PUBLIC"))
    op.execute(text(_SQL))

    stmts = [_guarded(r, f"REVOKE ALL ON public.privacy_legal_holds FROM {r};")
             for r in _LOCKED_OUT]
    for fn in (_PURGE, _HOLD):
        stmts.append(f"REVOKE ALL ON FUNCTION public.{fn} FROM PUBLIC;")
        stmts += [_guarded(r, f"REVOKE ALL ON FUNCTION public.{fn} FROM {r};")
                  for r in _LOCKED_OUT]
    stmts.append(_guarded("bridgeleads_system",
                          f"GRANT EXECUTE ON FUNCTION public.{_PURGE} TO bridgeleads_system;"))
    op.execute(text("DO $privacy_138$\nBEGIN\n" + "\n".join(stmts) + "\nEND\n$privacy_138$;"))


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute(text("""
        DO $refuse$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.privacy_legal_holds) THEN
                RAISE EXCEPTION 'legal holds exist; refusing to drop them';
            END IF;
        END
        $refuse$
    """))
    op.execute(text(f"DROP FUNCTION IF EXISTS public.{_PURGE}"))
    op.execute(text(f"DROP FUNCTION IF EXISTS public.{_HOLD}"))
    op.execute(text("DROP TABLE public.privacy_legal_holds"))
