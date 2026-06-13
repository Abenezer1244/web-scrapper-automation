-- ============================================================================
-- create_owner_flag_indexes.sql — partial indexes for the owner-location filters
-- ----------------------------------------------------------------------------
-- Run OUT-OF-BAND (not in an Alembic migration): CREATE INDEX CONCURRENTLY can't
-- run inside a transaction, and a plain CREATE INDEX would take an ACCESS
-- EXCLUSIVE lock on the live 310k-row results table for the whole build (Codex).
-- CONCURRENTLY builds without blocking reads/writes.
--
-- Run AFTER migration 057 is deployed and the backfill has populated the flags,
-- as the owner/admin role on the SESSION pooler (:5432 — CONCURRENTLY needs a
-- real session, not the :6543 transaction pooler):
--
--   psql "$ADMIN_DATABASE_URL_SESSION" -f scripts/create_owner_flag_indexes.sql
--
-- Partial + (user_id) scoped: the filters always run inside a single tenant's
-- job (WHERE user_id = :uid AND absentee_owner IS TRUE), and only the TRUE rows
-- are ever selected by a "show me absentee" filter, so the index stays small.
-- IF NOT EXISTS makes this re-runnable. NULL/false rows are excluded from the
-- index entirely (they're never the target of a positive filter).
--
-- NOTE: CONCURRENTLY cannot run in a multi-statement transaction block; psql
-- runs each statement in its own implicit transaction, which is correct here.
-- If a build fails midway it leaves an INVALID index — drop it and re-run:
--   DROP INDEX CONCURRENTLY IF EXISTS ix_results_user_absentee;
-- ============================================================================

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_results_user_absentee
    ON results (user_id)
    WHERE absentee_owner IS TRUE;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_results_user_out_of_state
    ON results (user_id)
    WHERE out_of_state_owner IS TRUE;

-- NTS Tier 1 (migration 059): filter/sort upcoming trustee-sale auctions within a
-- job (days-to-auction). Deferred here for the same reason — a plain CREATE INDEX
-- in the migration would lock the live results table.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_results_job_auction_date
    ON results (job_id, auction_date)
    WHERE auction_date IS NOT NULL;
