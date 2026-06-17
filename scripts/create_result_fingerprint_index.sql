-- ============================================================================
-- create_result_fingerprint_index.sql — unique idempotency key for results
-- ----------------------------------------------------------------------------
-- Run OUT-OF-BAND (not in an Alembic migration): CREATE UNIQUE INDEX
-- CONCURRENTLY can't run inside a transaction, and a plain CREATE would take an
-- ACCESS EXCLUSIVE lock on the live ~310k-row results table for the whole build
-- (same rule as create_owner_flag_indexes.sql / migration 059). CONCURRENTLY
-- builds without blocking reads/writes.
--
-- DEPLOY ORDER (migration 062 — source_fingerprint dup-fix) on the LARGE prod
-- results table:
--   1. Run THIS script (adds the column IF NOT EXISTS + builds the index
--      CONCURRENTLY) on prod BEFORE `alembic upgrade head`.
--   2. Run migration 062 — it finds the column + index already present and
--      no-ops (on a large table it would otherwise RAISE rather than lock).
--   3. THEN ship the worker code that inserts with
--      ON CONFLICT (job_id, source_fingerprint) DO NOTHING.
-- (On a SMALL table — CI / fresh install — migration 062 builds the index inline
-- instead, so this script is only needed for the existing large prod table.)
--
-- Run as the owner/admin role on the SESSION pooler (:5432 — CONCURRENTLY needs
-- a real session, not the :6543 transaction pooler):
--
--   psql "$ADMIN_DATABASE_URL_SESSION" -f scripts/create_result_fingerprint_index.sql
--
-- PARTIAL (WHERE source_fingerprint IS NOT NULL): legacy rows (terminal jobs that
-- never re-run) have NULL fingerprints and are excluded entirely, so no backfill
-- is needed and the index only covers new rows. (job_id, source_fingerprint) is
-- the WITHIN-JOB row identity, so the uniqueness is scoped per job — a re-run of
-- the same job re-inserts the same fingerprints as ON CONFLICT no-ops instead of
-- appending a duplicate copy.
--
-- IF NOT EXISTS makes this re-runnable. A failed build leaves an INVALID index —
-- drop it and re-run:
--   DROP INDEX CONCURRENTLY IF EXISTS uq_results_job_fingerprint;
-- ============================================================================

-- Column first (IF NOT EXISTS) so this script can run BEFORE migration 062 on
-- prod — the index needs the column to exist. Instant, nullable ALTER.
ALTER TABLE results ADD COLUMN IF NOT EXISTS source_fingerprint TEXT;

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_results_job_fingerprint
    ON results (job_id, source_fingerprint)
    WHERE source_fingerprint IS NOT NULL;

-- Validity gate (Codex): a failed CONCURRENTLY build leaves an INVALID index, and
-- `IF NOT EXISTS` would then SKIP the rebuild on the next run — shipping worker
-- code whose ON CONFLICT has no usable arbiter (runtime error on every insert).
-- Fail the deploy loudly if the index is missing or not valid+ready. If this
-- fires: DROP INDEX CONCURRENTLY IF EXISTS uq_results_job_fingerprint; then re-run.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indexrelid
        WHERE c.relname = 'uq_results_job_fingerprint'
          AND i.indisvalid
          AND i.indisready
    ) THEN
        RAISE EXCEPTION
            'uq_results_job_fingerprint is missing or INVALID — drop it and '
            're-run this script BEFORE shipping the ON CONFLICT worker code';
    END IF;
END$$;
