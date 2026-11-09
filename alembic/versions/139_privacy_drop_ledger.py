"""California DROP ledger: what was downloaded, and each Email/Phone identifier (139).

P9 FOUNDATION ONLY (tasks/todo-privacy-p9-drop.md; the owner chose "foundation now, rest
later" after the Codex consult rejected the full design). It records what DROP sent. It
does NOT withhold, erase or report anything; nothing reads it but the DROP worker and the
operator's read-only match report. Dormant until DROP_ENABLED and DROP_API_KEY are set.

  drop_downloads     one row per completed pull: the ZIP's name (NULL when DROP had no new
                     data: the row is still evidence of the pull cadence), the per-list
                     row counts, the counts of lists we cannot match (NDZ, MAID, NameVIN,
                     CTVID: we hold no DOB, VIN or device IDs) and each list's CSV name
                     (the status file must reuse it).
  drop_identifiers   one row per Email/Phone work item: DROP's Id, its Base64 SHA-256
                     hash, when it arrived, when DROP withdrew it (the Removed file) and
                     its report-by time (download + 45 days, the regulation's window).

Functions (SECURITY DEFINER, search_path pinned, bridgeleads_system EXECUTE only):
  drop_begin_download()   records a pull; idempotent per ZIP name (returns the same id).
  drop_add_identifiers()  adds up to 5000 identifiers of one list to a download;
                          idempotent (a repeat inserts nothing). Returns rows inserted.
  drop_mark_removed()     stamps removed_at on up to 5000 identifiers matched by list, Id
                          AND hash. Returns rows stamped.
  drop_last_pull()        the time of the latest recorded pull (the worker's cadence).
  drop_pending_hashes()   keyset page of identifiers not withdrawn (id, list, hash), for
                          the read-only match report.

No role gains a table privilege. Downgrade refuses while any ledger row exists.

Revision ID: 139
Revises: 138
Create Date: 2026-11-09
"""
from alembic import op
from sqlalchemy import text

revision = "139"
down_revision = "138"
branch_labels = None
depends_on = None

_LOCKED_OUT = ("anon", "authenticated", "service_role", "bridgeleads_app",
               "bridgeleads_system", "bridgeleads_purge")
_TABLES = ("drop_downloads", "drop_identifiers")
_WORKER_FUNCTIONS = (
    "drop_begin_download(text, jsonb, jsonb, jsonb)",
    "drop_add_identifiers(uuid, text, text[], text[])",
    "drop_mark_removed(text, text[], text[])",
    "drop_last_pull()",
    "drop_pending_hashes(uuid, text, uuid, integer)",
)

_SQL = r"""
CREATE FUNCTION public.drop_begin_download(p_file_name text, p_list_counts jsonb,
                                           p_ignored_counts jsonb, p_list_files jsonb)
RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_id uuid;
BEGIN
    IF (p_file_name IS NOT NULL AND p_file_name !~ '^[A-Za-z0-9_.-]{1,96}\.zip$')
       OR jsonb_typeof(coalesce(p_list_counts, '{}')) <> 'object'
       OR jsonb_typeof(coalesce(p_ignored_counts, '{}')) <> 'object'
       OR jsonb_typeof(coalesce(p_list_files, '{}')) <> 'object'
       OR length(coalesce(p_list_counts, '{}')::text) > 1024
       OR length(coalesce(p_ignored_counts, '{}')::text) > 1024
       OR length(coalesce(p_list_files, '{}')::text) > 1024 THEN
        RAISE EXCEPTION 'drop_begin_download: invalid input' USING ERRCODE = 'BLD61';
    END IF;
    IF p_file_name IS NOT NULL THEN
        PERFORM pg_advisory_xact_lock(hashtextextended('drop_download:' || p_file_name, 0));
        SELECT d.id INTO v_id FROM public.drop_downloads d WHERE d.file_name = p_file_name;
        IF v_id IS NOT NULL THEN
            RETURN v_id;
        END IF;
    END IF;
    INSERT INTO public.drop_downloads (file_name, list_counts, ignored_counts, list_files)
    VALUES (p_file_name, coalesce(p_list_counts, '{}'), coalesce(p_ignored_counts, '{}'),
            coalesce(p_list_files, '{}'))
    RETURNING id INTO v_id;
    RETURN v_id;
END
$fn$;

CREATE FUNCTION public.drop_add_identifiers(p_download uuid, p_list_type text,
                                            p_ids text[], p_hashes text[])
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_at timestamptz;
    v_n integer;
BEGIN
    IF p_list_type NOT IN ('email', 'phone')
       OR p_ids IS NULL OR p_hashes IS NULL
       OR cardinality(p_ids) <> cardinality(p_hashes) OR cardinality(p_ids) > 5000 THEN
        RAISE EXCEPTION 'drop_add_identifiers: invalid input' USING ERRCODE = 'BLD61';
    END IF;
    SELECT d.downloaded_at INTO v_at FROM public.drop_downloads d
     WHERE d.id = p_download AND d.file_name IS NOT NULL;
    IF v_at IS NULL THEN
        RAISE EXCEPTION 'drop_add_identifiers: no such download' USING ERRCODE = 'BLD61';
    END IF;
    -- Malformed ids or hashes fail the table's CHECKs: the whole batch is refused.
    INSERT INTO public.drop_identifiers (download_id, list_type, drop_id, hash, due_at)
    SELECT p_download, p_list_type, i.id, i.hash, v_at + interval '45 days'
      FROM unnest(p_ids, p_hashes) AS i(id, hash)
    ON CONFLICT (download_id, list_type, drop_id) DO NOTHING;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n;
END
$fn$;

CREATE FUNCTION public.drop_mark_removed(p_list_type text, p_ids text[], p_hashes text[])
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
DECLARE
    v_n integer;
BEGIN
    IF p_list_type NOT IN ('email', 'phone')
       OR p_ids IS NULL OR p_hashes IS NULL
       OR cardinality(p_ids) <> cardinality(p_hashes) OR cardinality(p_ids) > 5000 THEN
        RAISE EXCEPTION 'drop_mark_removed: invalid input' USING ERRCODE = 'BLD61';
    END IF;
    UPDATE public.drop_identifiers t SET removed_at = now()
      FROM unnest(p_ids, p_hashes) AS i(id, hash)
     WHERE t.list_type = p_list_type AND t.drop_id = i.id AND t.hash = i.hash
       AND t.removed_at IS NULL;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n;
END
$fn$;

CREATE FUNCTION public.drop_last_pull() RETURNS timestamptz
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    SELECT max(d.downloaded_at) FROM public.drop_downloads d
$fn$;

CREATE FUNCTION public.drop_pending_hashes(p_download uuid, p_list_type text,
                                           p_after uuid, p_limit integer)
RETURNS TABLE (id uuid, list_type text, hash text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $fn$
    SELECT t.id, t.list_type::text, t.hash::text FROM public.drop_identifiers t
     WHERE t.removed_at IS NULL
       AND (p_download IS NULL OR t.download_id = p_download)
       AND (p_list_type IS NULL OR t.list_type = p_list_type)
       AND (p_after IS NULL OR t.id > p_after)
     ORDER BY t.id
     LIMIT least(greatest(coalesce(p_limit, 1), 1), 10000)
$fn$;
"""


def _guarded(role: str, stmt: str) -> str:
    return (f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN {stmt} "
            "END IF;")


def upgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute(text("""
        CREATE TABLE public.drop_downloads (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            file_name varchar(100),
            downloaded_at timestamptz NOT NULL DEFAULT now(),
            list_counts jsonb NOT NULL DEFAULT '{}',
            ignored_counts jsonb NOT NULL DEFAULT '{}',
            list_files jsonb NOT NULL DEFAULT '{}',
            CONSTRAINT ck_drop_downloads_file_name
                CHECK (file_name ~ '^[A-Za-z0-9_.-]{1,96}\\.zip$')
        )
    """))
    op.execute(text("CREATE UNIQUE INDEX uq_drop_downloads_file_name "
                    "ON public.drop_downloads (file_name) WHERE file_name IS NOT NULL"))
    op.execute(text("""
        CREATE TABLE public.drop_identifiers (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            download_id uuid NOT NULL REFERENCES public.drop_downloads(id) ON DELETE RESTRICT,
            list_type varchar(8) NOT NULL,
            drop_id varchar(12) NOT NULL,
            hash varchar(44) NOT NULL,
            received_at timestamptz NOT NULL DEFAULT now(),
            removed_at timestamptz,
            due_at timestamptz NOT NULL,
            CONSTRAINT uq_drop_identifiers UNIQUE (download_id, list_type, drop_id),
            CONSTRAINT ck_drop_identifiers_list CHECK (list_type IN ('email', 'phone')),
            CONSTRAINT ck_drop_identifiers_id CHECK (drop_id ~ '^[A-Za-z0-9]{12}$'),
            CONSTRAINT ck_drop_identifiers_hash
                CHECK (CASE WHEN hash ~ '^[A-Za-z0-9+/]{43}=$'
                           THEN length(decode(hash, 'base64')) = 32 ELSE false END)
        )
    """))
    op.execute(text("CREATE INDEX ix_drop_identifiers_hash "
                    "ON public.drop_identifiers (list_type, hash)"))
    for table in _TABLES:
        op.execute(text(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY"))
        op.execute(text(f"REVOKE ALL ON public.{table} FROM PUBLIC"))
    op.execute(text(_SQL))

    stmts = [_guarded(r, f"REVOKE ALL ON public.{t} FROM {r};")
             for t in _TABLES for r in _LOCKED_OUT]
    for fn in _WORKER_FUNCTIONS:
        stmts.append(f"REVOKE ALL ON FUNCTION public.{fn} FROM PUBLIC;")
        stmts += [_guarded(r, f"REVOKE ALL ON FUNCTION public.{fn} FROM {r};")
                  for r in _LOCKED_OUT]
        stmts.append(_guarded("bridgeleads_system",
                              f"GRANT EXECUTE ON FUNCTION public.{fn} TO bridgeleads_system;"))
    op.execute(text("DO $privacy_139$\nBEGIN\n" + "\n".join(stmts) + "\nEND\n$privacy_139$;"))


def downgrade() -> None:
    op.execute(text("SET LOCAL lock_timeout = '5s'"))
    op.execute(text("""
        DO $refuse$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.drop_downloads) THEN
                RAISE EXCEPTION 'DROP ledger rows exist; refusing to drop them';
            END IF;
        END
        $refuse$
    """))
    for fn in _WORKER_FUNCTIONS:
        op.execute(text(f"DROP FUNCTION IF EXISTS public.{fn}"))
    op.execute(text("DROP TABLE public.drop_identifiers"))
    op.execute(text("DROP TABLE public.drop_downloads"))
