"""Drop user_record_views (120).

The table recorded each user's last view of the cached-records page, which UX 3.13
retired with its endpoint (GET /scrapers/{id}/records, 404 since 2026-10-11).
Nothing in the API or the workers reads or writes it; the last row is from
2026-09-25. Nothing references it (no inbound FK, view or other function).

The one thing that does name it is purge_account_data() (113, SECURITY DEFINER,
owned by bridgeleads_purge): one `DELETE FROM public.user_record_views ...` line.
Dropping the table first would make every account deletion fail at that line, so
this migration, in ONE transaction: rewrites the function without that line
(from its live definition, exactly one line removed, as its owner through 113's
temporary hand-over), checks the result (body, SECURITY DEFINER, owner and ACL
unchanged otherwise), and only then drops the table RESTRICT. Any failed check
raises, so the function and the table are left untouched.

Downgrade recreates the table as 023 + the RLS cutover + 113 left it (columns,
constraints, forced RLS, the _app / _system / _purge policies, the grants, the
113 fence trigger) and puts the DELETE line back. It restores no rows: the
pre-merge backup is the data rollback.

Revision ID: 120
Revises: 119
Create Date: 2026-10-21
"""
from alembic import op
from sqlalchemy import text

revision = "120"
down_revision = "119"
branch_labels = None
depends_on = None

_FN = "public.purge_account_data(uuid,uuid,text[],integer)"
_LINE = "DELETE FROM public.user_record_views WHERE user_id = v_uid;"
_ANCHOR = "DELETE FROM public.notifications WHERE user_id = v_uid;"
_GUC = "user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid"


def _fail(msg: str) -> None:
    raise RuntimeError(f"120: {msg}; nothing was changed")


def _fn_state(bind) -> tuple[str, str, bool, str]:
    return tuple(bind.execute(text(
        f"SELECT pg_get_functiondef('{_FN}'::regprocedure), pg_get_userbyid(proowner), prosecdef,"
        f" coalesce(array_to_string(proacl, ','), '') FROM pg_proc WHERE oid = '{_FN}'::regprocedure"
    )).one())


def _replace_as_owner(bind, definition: str) -> None:
    """CREATE OR REPLACE the function as bridgeleads_purge, 113's hand-over (owner, ACL kept)."""
    superuser = bind.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")).scalar()
    if not superuser:
        op.execute("GRANT bridgeleads_purge TO CURRENT_USER WITH SET TRUE, INHERIT FALSE")
    op.execute("GRANT CREATE ON SCHEMA public TO bridgeleads_purge")
    op.execute("SET LOCAL ROLE bridgeleads_purge")
    op.execute(definition)
    op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA public FROM bridgeleads_purge")
    if not superuser:
        op.execute("REVOKE bridgeleads_purge FROM CURRENT_USER")
    stray = bind.execute(text(
        "SELECT count(*) FROM pg_auth_members m WHERE m.roleid = 'bridgeleads_purge'::regrole"
        " AND (m.member <> current_user::regrole OR m.set_option OR m.inherit_option)"
    )).scalar()
    if stray:
        _fail("unexpected membership in bridgeleads_purge")


def _rewrite(bind, edit) -> None:
    before, owner, secdef, acl = _fn_state(bind)
    after = edit(before)
    _replace_as_owner(bind, after)
    now = _fn_state(bind)
    if now != (after, owner, secdef, acl):
        _fail(f"purge_account_data did not come back as intended (owner/secdef/acl {now[1:]} vs {(owner, secdef, acl)})")


def _without_line(definition: str) -> str:
    lines = definition.split("\n")
    hits = [i for i, ln in enumerate(lines) if ln.strip() == _LINE]
    if len(hits) != 1:
        _fail(f"expected the user_record_views DELETE once in purge_account_data, found {len(hits)}")
    del lines[hits[0]]
    out = "\n".join(lines)
    if "user_record_views" in out:
        _fail("purge_account_data still names user_record_views")
    return out


def _with_line(definition: str) -> str:
    lines = definition.split("\n")
    hits = [i for i, ln in enumerate(lines) if ln.strip() == _ANCHOR]
    if len(hits) != 1 or any(ln.strip() == _LINE for ln in lines):
        _fail("cannot put the user_record_views DELETE back after the notifications DELETE")
    indent = lines[hits[0]][: len(lines[hits[0]]) - len(lines[hits[0]].lstrip())]
    lines.insert(hits[0] + 1, indent + _LINE)
    return "\n".join(lines)


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    bind = op.get_bind()
    _rewrite(bind, _without_line)
    op.execute("DROP TABLE public.user_record_views RESTRICT")


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute(
        """
        CREATE TABLE public.user_record_views (
            id uuid DEFAULT gen_random_uuid() NOT NULL,
            user_id uuid NOT NULL,
            scraper_config_id uuid NOT NULL,
            last_viewed_at timestamptz DEFAULT now() NOT NULL,
            created_at timestamptz DEFAULT now() NOT NULL,
            CONSTRAINT user_record_views_pkey PRIMARY KEY (id),
            CONSTRAINT uq_user_config_view UNIQUE (user_id, scraper_config_id),
            CONSTRAINT user_record_views_user_id_fkey FOREIGN KEY (user_id)
                REFERENCES public.users(id) ON DELETE CASCADE,
            CONSTRAINT user_record_views_scraper_config_id_fkey FOREIGN KEY (scraper_config_id)
                REFERENCES public.scraper_configs(id) ON DELETE CASCADE
        )
        """
    )
    op.execute("ALTER TABLE public.user_record_views ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.user_record_views FORCE ROW LEVEL SECURITY")
    op.execute("GRANT SELECT, DELETE ON public.user_record_views TO bridgeleads_purge")
    op.execute(
        "CREATE POLICY user_record_views_purge ON public.user_record_views FOR ALL TO bridgeleads_purge"
        " USING (true) WITH CHECK (true)"
    )
    op.execute(
        f"""
        DO $urv_roles$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_app') THEN
                GRANT SELECT, INSERT, UPDATE ON public.user_record_views TO bridgeleads_app;
                CREATE POLICY user_record_views_app ON public.user_record_views TO bridgeleads_app
                    USING ({_GUC}) WITH CHECK ({_GUC});
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_system') THEN
                GRANT SELECT, INSERT, UPDATE ON public.user_record_views TO bridgeleads_system;
                CREATE POLICY user_record_views_system ON public.user_record_views
                    FOR ALL TO bridgeleads_system USING (true) WITH CHECK (true);
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bridgeleads_rls_test') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON public.user_record_views TO bridgeleads_rls_test;
            END IF;
        END
        $urv_roles$;
        """
    )
    op.execute(
        "CREATE TRIGGER zz_account_deletion_fence BEFORE INSERT OR UPDATE ON public.user_record_views"
        " FOR EACH ROW EXECUTE FUNCTION public.account_deletion_fence()"
    )
    op.execute("ALTER TABLE public.user_record_views ENABLE ALWAYS TRIGGER zz_account_deletion_fence")
    _rewrite(op.get_bind(), _with_line)
