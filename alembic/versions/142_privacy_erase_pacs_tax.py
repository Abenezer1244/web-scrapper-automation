"""Erasure reaches pacs_tax_status (the Asotin per-parcel tax cache, migration 141).

pacs_tax_status holds an owner name and addresses for parcels that owe past-due tax,
so a deletion rule must be able to erase them like king_parcel_owners and nts_notices.
Rather than redefine 135's audited privacy_erase_batch, this adds one small companion
with the same contract: SECURITY DEFINER, owned by bridgeleads_purge, bounded batch,
only an ACTIVE, never-lifted DELETION rule (privacy_erasure_rule), the same per-rule
advisory lock, executable only by bridgeleads_system. src/privacy/erase.py calls it for
the "pacs_tax" store.

Revision ID: 142
Revises: 141
Create Date: 2026-11-12
"""
from alembic import op
from sqlalchemy import text

revision = "142"
down_revision = "141"
branch_labels = None
depends_on = None

_FN = "privacy_erase_pacs_tax(uuid, uuid[])"
_API_ROLES = ("anon", "authenticated", "service_role", "bridgeleads_app")

_FUNCTION_SQL = """
CREATE FUNCTION public.privacy_erase_pacs_tax(p_rule_row uuid, p_ids uuid[])
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_rule uuid;
    v_n integer;
BEGIN
    IF coalesce(cardinality(p_ids), 0) > 500 THEN
        RAISE EXCEPTION 'privacy_erase_pacs_tax: batch too large' USING ERRCODE = 'BLD40';
    END IF;
    v_rule := public.privacy_erasure_rule(p_rule_row);
    IF v_rule IS NULL THEN
        RAISE EXCEPTION 'privacy_erase_pacs_tax: not an active deletion rule'
              USING ERRCODE = 'BLD41';
    END IF;
    PERFORM pg_advisory_xact_lock(7135, hashtext(v_rule::text));  -- as privacy_erase_batch
    UPDATE public.pacs_tax_status p SET
        owner_name = NULL, property_address = NULL, mailing_address = NULL,
        privacy_suppression_id = p_rule_row
     WHERE p.id = ANY(coalesce(p_ids, '{}')) AND p.privacy_suppression_id IS NULL;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN jsonb_build_object('pacs_tax', v_n);
END
$fn$;
"""


def _guarded(role: str, stmt: str) -> str:
    return (f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN {stmt} "
            "END IF;")


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute(text(_FUNCTION_SQL))
    stmts = [
        "GRANT SELECT, UPDATE (owner_name, property_address, mailing_address, "
        "privacy_suppression_id) ON public.pacs_tax_status TO bridgeleads_purge;",
        "DROP POLICY IF EXISTS pacs_tax_status_purge ON public.pacs_tax_status;",
        "CREATE POLICY pacs_tax_status_purge ON public.pacs_tax_status FOR ALL "
        "TO bridgeleads_purge USING (true) WITH CHECK (true);",
        f"REVOKE ALL ON FUNCTION public.{_FN} FROM PUBLIC;",
        *[_guarded(r, f"REVOKE ALL ON FUNCTION public.{_FN} FROM {r};")
          for r in (*_API_ROLES, "bridgeleads_system")],
        # Granted before the hand-over: ALTER OWNER carries the old owner's grants across.
        _guarded("bridgeleads_system",
                 f"GRANT EXECUTE ON FUNCTION public.{_FN} TO bridgeleads_system;"),
    ]
    body = "\n".join(stmts)
    op.execute(text(f"""
        DO $grants$
        DECLARE
            v_super boolean;
        BEGIN
            {body}
            -- Same temporary hand-over as 135: SET on the purge role and CREATE on
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
            "SELECT 1 FROM public.pacs_tax_status WHERE privacy_suppression_id IS NOT NULL "
            "AND owner_name IS NULL LIMIT 1")).first():
        raise RuntimeError("erased pacs_tax_status rows exist; refusing to downgrade 142")
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
            DROP POLICY IF EXISTS pacs_tax_status_purge ON public.pacs_tax_status;
            REVOKE ALL ON public.pacs_tax_status FROM bridgeleads_purge;
        END
        $drop$
    """))
