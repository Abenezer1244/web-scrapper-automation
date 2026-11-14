"""probate_notices: published probate notices to creditors (Asotin first), with erasure.

A shared, system-only cache like nts_notices (058) and pacs_tax_status (151): the
nwmarket crawler stores each Asotin notice and the parcels tied to the estate at crawl
time (src/scrapers/enrichment/asotin_parcels.py); the Asotin probate scraper reads it.

Personal data: the decedent, the personal representatives (used only to decide the
family tie, never delivered), the claims address and the tied parcels. Erasure reaches
the table through ``privacy_erase_probate``, the same contract as 152's companion:
SECURITY DEFINER owned by bridgeleads_purge, bounded batch, an ACTIVE never-lifted
DELETION rule only, the per-rule advisory lock, EXECUTE for bridgeleads_system only.
A CHECK keeps an erased (or otherwise withheld) notice nameless for good, so no later
crawl can write the person back (Codex consult).

Revision ID: 160
Revises: 157
Create Date: 2026-11-14
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import UUID

revision = "160"
down_revision = "157"
branch_labels = None
depends_on = None

_FN = "privacy_erase_probate(uuid, uuid[])"
_API_ROLES = ("anon", "authenticated", "service_role", "bridgeleads_app")
_PII = ("decedent", "personal_reps", "claims_address", "parcels")

_FUNCTION_SQL = """
CREATE FUNCTION public.privacy_erase_probate(p_rule_row uuid, p_ids uuid[])
RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_rule uuid;
    v_n integer;
BEGIN
    IF coalesce(cardinality(p_ids), 0) > 500 THEN
        RAISE EXCEPTION 'privacy_erase_probate: batch too large' USING ERRCODE = 'BLD40';
    END IF;
    v_rule := public.privacy_erasure_rule(p_rule_row);
    IF v_rule IS NULL THEN
        RAISE EXCEPTION 'privacy_erase_probate: not an active deletion rule'
              USING ERRCODE = 'BLD41';
    END IF;
    PERFORM pg_advisory_xact_lock(7135, hashtext(v_rule::text));  -- as privacy_erase_batch
    UPDATE public.probate_notices p SET
        decedent = NULL, personal_reps = NULL, claims_address = NULL, parcels = NULL,
        privacy_suppression_id = p_rule_row
     WHERE p.id = ANY(coalesce(p_ids, '{}')) AND p.privacy_suppression_id IS NULL;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN jsonb_build_object('probate', v_n);
END
$fn$;
"""


def _guarded(role: str, stmt: str) -> str:
    return (f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN {stmt} "
            "END IF;")


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.create_table(
        "probate_notices",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("county", sa.String(64), nullable=False),
        sa.Column("state", sa.String(4), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("cause_no", sa.String(32), nullable=False),
        sa.Column("first_published", sa.Date, nullable=False),
        sa.Column("decedent", sa.String(256), nullable=True),
        sa.Column("personal_reps", sa.JSON, nullable=True),
        sa.Column("claims_address", sa.String(256), nullable=True),
        sa.Column("advert_id", sa.String(32), nullable=True),
        sa.Column("pdf_url", sa.String(512), nullable=True),
        # matched | held | failed (the layer gave no answer: retried)
        sa.Column("match_outcome", sa.String(16), nullable=True),
        sa.Column("held_reasons", sa.JSON, nullable=True),
        sa.Column("parcels", sa.JSON, nullable=True),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("privacy_suppression_id", UUID(as_uuid=False),
                  sa.ForeignKey("privacy_suppressions.id", ondelete="RESTRICT"), nullable=True),
        sa.UniqueConstraint("county", "state", "cause_no", name="uq_probate_notices_cause"),
        sa.CheckConstraint(
            "privacy_suppression_id IS NULL OR (" + " AND ".join(f"{c} IS NULL" for c in _PII) + ")",
            name="ck_probate_notices_privacy_withheld"),
    )
    op.create_index("ix_probate_notices_published", "probate_notices",
                    ["county", "state", "first_published"])

    op.execute("ALTER TABLE probate_notices ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        DO $pn_rls$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON probate_notices TO bridgeleads_system;
                DROP POLICY IF EXISTS probate_notices_system ON probate_notices;
                CREATE POLICY probate_notices_system ON probate_notices
                    FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true);
            END IF;
        END
        $pn_rls$;
        """
    )

    op.execute(text(_FUNCTION_SQL))
    stmts = [
        "GRANT SELECT, UPDATE (" + ", ".join(_PII) + ", privacy_suppression_id) "
        "ON public.probate_notices TO bridgeleads_purge;",
        "DROP POLICY IF EXISTS probate_notices_purge ON public.probate_notices;",
        "CREATE POLICY probate_notices_purge ON public.probate_notices FOR ALL "
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
            -- Same temporary hand-over as 135/152: SET on the purge role and CREATE on
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
            "SELECT 1 FROM public.probate_notices WHERE privacy_suppression_id IS NOT NULL "
            "LIMIT 1")).first():
        raise RuntimeError("erased probate_notices rows exist; refusing to downgrade 160")
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
    op.execute("DROP POLICY IF EXISTS probate_notices_purge ON probate_notices")
    op.execute("DROP POLICY IF EXISTS probate_notices_system ON probate_notices")
    op.drop_index("ix_probate_notices_published", table_name="probate_notices")
    op.drop_table("probate_notices")
