-- ============================================================================
-- create_delivered_records_first_job_id_index.sql
-- ----------------------------------------------------------------------------
-- Run OUT-OF-BAND (not in an Alembic migration): CREATE INDEX CONCURRENTLY can't
-- run inside a transaction, and a plain CREATE would take an ACCESS EXCLUSIVE
-- lock on the live ~81k-row delivered_records table for the whole build (the
-- 2026-06-13 backfill-vs-ALTER lock incident; same rule as
-- create_result_fingerprint_index.sql / migration 062). CONCURRENTLY builds
-- without blocking reads/writes.
--
-- WHY: PR #59's idempotent cross-job dedup added a Step 2b query
--   SELECT dedup_hash FROM delivered_records WHERE first_job_id = :jid
-- that runs on every scrape (re-claims rows this job already owns on a watchdog
-- re-run). first_job_id is a plain UUID column (not a FK) and was unindexed, so
-- the query seq-scanned the whole table. This is a FILTER-ONLY index (not an ON
-- CONFLICT arbiter): its absence only slows the query, it never breaks anything.
--
-- DEPLOY ORDER (migration 064) on the LARGE prod table:
--   1. Run THIS script (builds the index CONCURRENTLY) on prod BEFORE
--      `alembic upgrade head`.
--   2. Run migration 064 — it finds the index present+valid and no-ops.
-- (On a SMALL table — CI / fresh install — migration 064 builds it inline.)
--
-- Run as the owner/admin role on the SESSION pooler (:5432 — CONCURRENTLY needs a
-- real session, not the :6543 transaction pooler):
--
--   psql "$ADMIN_DATABASE_URL_SESSION" -f scripts/create_delivered_records_first_job_id_index.sql
--
-- IF NOT EXISTS makes this re-runnable. A failed build leaves an INVALID index —
-- drop it and re-run:
--   DROP INDEX CONCURRENTLY IF EXISTS ix_delivered_records_first_job_id;
-- ============================================================================

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_delivered_records_first_job_id
    ON delivered_records (first_job_id);

-- Validity gate: a failed CONCURRENTLY build leaves an INVALID index, and
-- `IF NOT EXISTS` would then SKIP the rebuild on the next run. Fail loudly if the
-- index is missing or not valid+ready. If this fires:
--   DROP INDEX CONCURRENTLY IF EXISTS ix_delivered_records_first_job_id; then re-run.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indexrelid
        WHERE c.relname = 'ix_delivered_records_first_job_id'
          AND i.indisvalid
          AND i.indisready
    ) THEN
        RAISE EXCEPTION
            'ix_delivered_records_first_job_id is missing or INVALID — drop it '
            'and re-run this script BEFORE running migration 064';
    END IF;
END$$;
