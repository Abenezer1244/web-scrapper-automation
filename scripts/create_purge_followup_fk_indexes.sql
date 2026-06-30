-- ============================================================================
-- create_purge_followup_fk_indexes.sql
-- ----------------------------------------------------------------------------
-- Run OUT-OF-BAND (not in an Alembic migration): CREATE INDEX CONCURRENTLY can't
-- run inside a transaction, and a plain CREATE would take an ACCESS EXCLUSIVE
-- lock on the live table for the whole build (the 2026-06-13 backfill-vs-ALTER
-- lock incident; same rule as create_delivered_records_first_job_id_index.sql /
-- migration 064). CONCURRENTLY builds without blocking reads/writes.
--
-- WHY: the 2026-06-22 test-data purge (delete all users except admin) timed out
-- because these FK columns pointing at results/jobs were UNINDEXED, so each row
-- deletion seq-scanned a sibling table once per parent row. The worst:
--   delivered_records.first_result_id -> results(id) ON DELETE SET NULL
-- meant every result deletion fired
--   UPDATE delivered_records SET first_result_id = NULL WHERE first_result_id = $1
-- a full seq-scan of delivered_records PER deleted result. This is a latent perf
-- bug in normal operation too, not just for the purge. All four are FILTER/cascade
-- -only indexes (not ON CONFLICT arbiters): their absence only slows cascades.
--
-- DEPLOY ORDER (migration 069) on LARGE prod tables:
--   1. Run THIS script (builds the indexes CONCURRENTLY) on prod BEFORE
--      `alembic upgrade head`.
--   2. Run migration 069 — it finds them present+valid and no-ops.
-- (On SMALL tables — CI / fresh install — migration 069 builds them inline.)
--
-- Run as the owner/admin role on the SESSION pooler (:5432 — CONCURRENTLY needs a
-- real session, not the :6543 transaction pooler):
--
--   psql "$ADMIN_DATABASE_URL_SESSION" -f scripts/create_purge_followup_fk_indexes.sql
--
-- IF NOT EXISTS makes this re-runnable. A failed build leaves an INVALID index —
-- drop it (DROP INDEX CONCURRENTLY IF EXISTS <name>;) and re-run.
-- ============================================================================

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_delivered_records_first_result_id
    ON delivered_records (first_result_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_pending_skip_trace_rows_result_id
    ON pending_skip_trace_rows (result_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_pending_skip_trace_rows_job_id
    ON pending_skip_trace_rows (job_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_job_logs_job_id
    ON job_logs (job_id);

-- Validity gate: a failed CONCURRENTLY build leaves an INVALID index, and
-- `IF NOT EXISTS` would then SKIP the rebuild on the next run. Fail loudly if any
-- of the four is missing or not valid+ready. If this fires, drop that index
-- (DROP INDEX CONCURRENTLY IF EXISTS <name>;) and re-run this script BEFORE
-- running migration 069.
DO $$
DECLARE
    idx text;
BEGIN
    FOREACH idx IN ARRAY ARRAY[
        'ix_delivered_records_first_result_id',
        'ix_pending_skip_trace_rows_result_id',
        'ix_pending_skip_trace_rows_job_id',
        'ix_job_logs_job_id'
    ] LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            WHERE c.relname = idx AND i.indisvalid AND i.indisready
        ) THEN
            RAISE EXCEPTION '% is missing or INVALID — drop it and re-run this script BEFORE migration 069', idx;
        END IF;
    END LOOP;
END$$;
