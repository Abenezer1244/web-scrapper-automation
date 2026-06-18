"""Per-job source fingerprint for idempotent result inserts. (062)

run_scrape_job re-runs (Celery hard-kill + watchdog re-queue) used to APPEND a
second full copy of a job's results because the insert had no idempotency key
(the 2026-06-17 duplication incident, x2-x6 on large King tax runs). This adds a
deterministic per-record fingerprint so the insert can be
`ON CONFLICT (job_id, source_fingerprint) DO NOTHING` — a re-run re-inserts the
SAME rows as no-ops instead of appending duplicates.

The fingerprint is computed in the worker at insert time from the record's
SCRAPE-TIME source identity: the scraper's own raw_html_hash when set (the
template scrapers' deterministic in-memory dedup key), else a SHA-256 over a
canonical tuple (record_type, parcel_id, date_recorded, doc_type, party_name,
legal_description, property_address). It deliberately EXCLUDES enrichment_data
and mailing_address (filled/normalized during enrichment) so the key can't drift
between a run and its re-run. It is the WITHIN-JOB row identity; it differs from
dedup_hash (the FROZEN cross-job billing-dedup key, parcel|address) and
property_key (the overlap key) — a parcel with multiple distinct filings yields
multiple results, each with its own fingerprint.

NULLABLE = instant ALTER on the RLS-FORCE'd results table (no rewrite, no policy
change). NO backfill: existing rows belong to terminal jobs that never re-run,
and Postgres treats NULL fingerprints as distinct in a unique index, so legacy
NULL rows coexist with the new constraint.

The UNIQUE index is the ON CONFLICT ARBITER, so unlike the filter-only NTS/owner
indexes (whose absence merely slows a query) it MUST exist wherever run_scrape_job
runs — a missing arbiter ERRORS on every insert. But a plain CREATE on the live
~310k results table would scan + ACCESS-EXCLUSIVE-lock it inside the alembic txn
(the 2026-06-13 backfill-vs-ALTER lock incident). So:

  - LARGE table (prod): build the index OUT-OF-BAND, CONCURRENTLY, via
    scripts/create_result_fingerprint_index.sql, which also adds the column
    (IF NOT EXISTS), BEFORE running this migration. This migration then finds the
    index already present and no-ops. If it is NOT present on a large table, this
    migration RAISES — a loud deploy failure beats silently shipping a worker
    whose ON CONFLICT has no arbiter.
  - SMALL table (test / CI / fresh install): no lock concern, so this migration
    builds the index inline (instant on an empty/tiny table) — so `alembic upgrade
    head` alone gives CI and fresh environments a working ON CONFLICT.

Revision ID: 062
Revises: 061
Create Date: 2026-06-17
"""
from alembic import op
from sqlalchemy import text

revision = "062"
down_revision = "061"
branch_labels = None
depends_on = None

_INDEX = "uq_results_job_fingerprint"
# Below this row estimate the inline (ACCESS EXCLUSIVE) build is effectively
# instant and safe; above it we require the CONCURRENTLY out-of-band build.
_SMALL_TABLE_ROWS = 50_000


def upgrade() -> None:
    conn = op.get_bind()
    # IF NOT EXISTS: the out-of-band prod script may have already added the column.
    conn.execute(text("ALTER TABLE results ADD COLUMN IF NOT EXISTS source_fingerprint TEXT"))

    # Check VALIDITY, not mere existence (Codex): a failed CONCURRENTLY build
    # leaves an index that exists but is invalid/not-ready — ON CONFLICT can't use
    # it, so no-oping on name-only presence would ship a broken arbiter.
    index_valid = conn.execute(
        text(
            "SELECT i.indisvalid AND i.indisready FROM pg_class c "
            "JOIN pg_index i ON i.indexrelid = c.oid WHERE c.relname = :n"
        ),
        {"n": _INDEX},
    ).scalar()
    if index_valid is True:
        return  # built out-of-band and valid — nothing to do
    if index_valid is False:
        raise RuntimeError(
            f"{_INDEX} exists but is INVALID/not-ready (a failed CONCURRENTLY "
            f"build). DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}; then rebuild it "
            "out-of-band (large table) or re-run this migration (small table)."
        )
    # index_valid is None → the index does not exist → build it below.

    # reltuples is -1 on a never-analyzed table and 0 on a fresh/empty one; both
    # are "small". A populated prod table reports its ~row estimate.
    est = conn.execute(
        text("SELECT reltuples::bigint FROM pg_class WHERE relname = 'results'")
    ).scalar()
    est = 0 if est is None or est < 0 else est

    if est < _SMALL_TABLE_ROWS:
        op.create_index(
            _INDEX,
            "results",
            ["job_id", "source_fingerprint"],
            unique=True,
            postgresql_where=text("source_fingerprint IS NOT NULL"),
        )
    else:
        raise RuntimeError(
            f"results has ~{est} rows: build {_INDEX} CONCURRENTLY out-of-band "
            "(scripts/create_result_fingerprint_index.sql) BEFORE running this "
            "migration — an inline CREATE would ACCESS-EXCLUSIVE-lock the live table."
        )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="results", if_exists=True)
    op.drop_column("results", "source_fingerprint")
