"""One active job per scraper config, enforced by the database (104).

UX audit F-003 / Phase 3.0 Q5. POST /jobs ("Run now") never checked for a job
already active on the same config; only the scheduler did. Two concurrent runs of
one config both scrape the county, and the delivered_records ON CONFLICT dedup
splits the new leads between them by race, so each run's page shows part of the
set and calls the rest "already delivered".

The partial unique index below makes a second ACTIVE job for one config
impossible on every path at once: the API, the scheduler, the batch fan-out and
any script. The API's pre-check and 409 are the friendly face of it; this index
is the authority.

Compatibility, checked when this was written:
  * The watchdog re-queue UPDATEs the same row back to 'pending'; it never
    inserts, so it cannot collide with itself.
  * The scheduler inserts with ON CONFLICT DO NOTHING and no conflict target, so
    a clash with this index is skipped rather than raised.
  * The batch fan-out inserts each child in a SAVEPOINT and reports a clash as
    "already running" (src/workers/batch_tasks.py).

ACTIVE here must equal src.config.constants.ACTIVE_STATUSES. It is duplicated,
not imported, because a migration must keep working when the application code
moves on; tests/test_run_in_flight_guard.py asserts the two agree.

Lock safety: built CONCURRENTLY in an autocommit block with a lock timeout, and
every step is idempotent (the 098/100 restart-safety pattern). jobs held 136
rows in production when this was written, so the build is instant.

Downgrade removes the invariant: after it, two concurrent runs of one config are
possible again through POST /jobs.

Revision ID: 104
Revises: 103
Create Date: 2026-09-27
"""
from alembic import op
from sqlalchemy import text

revision = "104"
down_revision = "103"
branch_labels = None
depends_on = None

_INDEX = "uq_jobs_one_active_per_config"
_ACTIVE = "('pending','queued','probing','scraping','enriching')"

# How Postgres renders the WHERE clause below back from the catalog (read from a
# real build, not written by hand).
_EXPECTED_PREDICATE = (
    "((status)::text = ANY ((ARRAY['pending'::character varying, "
    "'queued'::character varying, 'probing'::character varying, "
    "'scraping'::character varying, 'enriching'::character varying])::text[]))"
)


def upgrade() -> None:
    conn = op.get_bind()

    # Guard BEFORE the build, so a duplicate produces this instruction rather
    # than a bare "could not create unique index" and a half-built index.
    duplicates = conn.execute(text(
        f"SELECT count(*) FROM (SELECT scraper_config_id FROM public.jobs "
        f"WHERE status IN {_ACTIVE} GROUP BY scraper_config_id HAVING count(*) > 1) t"
    )).scalar()
    if duplicates:
        raise RuntimeError(
            f"Migration 104 ABORTED: {duplicates} scraper config(s) already have more "
            f"than one ACTIVE job. Do not cancel either by age: find which one holds "
            f"the config's newest delivered_records claims, let it finish or cancel "
            f"the other deliberately, then re-run. See tasks/todo-run-in-flight-guard.md."
        )

    with op.get_context().autocommit_block():
        conn = op.get_bind()
        conn.execute(text("SET lock_timeout = '5s'"))
        try:
            # A failed CONCURRENTLY build leaves an INVALID index behind, and a
            # same-named index of the wrong shape would let IF NOT EXISTS record
            # 104 as applied while nothing is enforced. Check identity, not just
            # the name, and drop a wrong or dead one before building. Safe here
            # because migrations are serialized by scripts/migrate.py's advisory
            # lock, so an invalid index at this point is dead, not building.
            existing = conn.execute(text(
                "SELECT i.indisvalid, i.indisunique, i.indnatts, i.indnkeyatts, "
                "       i.indexprs IS NULL AS plain_columns, a.attname, "
                "       pg_get_expr(i.indpred, i.indrelid) AS predicate "
                "FROM pg_class c "
                "JOIN pg_namespace cn ON cn.oid = c.relnamespace "
                "JOIN pg_index i ON i.indexrelid = c.oid "
                "JOIN pg_class t ON t.oid = i.indrelid "
                "JOIN pg_namespace tn ON tn.oid = t.relnamespace "
                "LEFT JOIN pg_attribute a "
                "       ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] "
                "WHERE c.relname = :n AND cn.nspname = 'public' "
                "  AND t.relname = 'jobs' AND tn.nspname = 'public'"
            ), {"n": _INDEX}).first()
            wrong_shape = existing is not None and not (
                existing.indisvalid
                and existing.indisunique
                and existing.indnkeyatts == 1
                and existing.indnatts >= existing.indnkeyatts
                and existing.plain_columns
                and existing.attname == "scraper_config_id"
                and " ".join((existing.predicate or "").split())
                == " ".join(_EXPECTED_PREDICATE.split())
            )
            if wrong_shape:
                conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS public.{_INDEX}"))
            elif existing is None:
                # Right name on some OTHER public table: never drop what may be
                # something else's. Refuse and let a human look.
                collision = conn.execute(text(
                    "SELECT tn.nspname || '.' || t.relname "
                    "FROM pg_class c "
                    "JOIN pg_namespace cn ON cn.oid = c.relnamespace "
                    "JOIN pg_index i ON i.indexrelid = c.oid "
                    "JOIN pg_class t ON t.oid = i.indrelid "
                    "JOIN pg_namespace tn ON tn.oid = t.relnamespace "
                    "WHERE c.relname = :n AND cn.nspname = 'public'"
                ), {"n": _INDEX}).scalar()
                if collision:
                    raise RuntimeError(
                        f"Migration 104 ABORTED: an index named {_INDEX} already "
                        f"exists on {collision}, which is not public.jobs. It is NOT "
                        f"dropped, because it may be something else's."
                    )
            # Fully qualified, so search_path cannot point it at a shadow table.
            conn.execute(text(
                f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
                f"ON public.jobs (scraper_config_id) "
                f"WHERE status IN {_ACTIVE}"
            ))
        finally:
            conn.execute(text("RESET lock_timeout"))


def downgrade() -> None:
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        conn.execute(text("SET lock_timeout = '5s'"))
        try:
            conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS public.{_INDEX}"))
        finally:
            conn.execute(text("RESET lock_timeout"))
