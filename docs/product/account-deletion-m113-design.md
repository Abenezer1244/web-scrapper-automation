## Migration 113 design (P3a, DB half of the purge)

Implements docs/product/account-deletion-retention-matrix.md. Builds on live 112
(bridgeleads_purge NOLOGIN owner role, users.deletion_state + guard trigger allowing
changes only when current_user = bridgeleads_purge, account_deletions with phase markers,
request/restore definer functions, lock order users row then account_deletions row).

### 1. Write fence
`account_deletion_fence()` SECURITY INVOKER, `SET search_path = pg_catalog, pg_temp`,
`BEFORE INSERT OR UPDATE FOR EACH ROW`, `ENABLE ALWAYS`, on: results, jobs, scraper_configs,
scraper_batches, batch_runs, notifications, user_record_views, property_list_membership,
dialer_deliveries, delivered_records, pending_skip_trace_rows, skip_trace_queues,
user_sessions, user_avatars, pending_email_changes, password_history, mfa_backup_codes,
mfa_break_glass_codes; plus `job_logs` (no user_id: resolves owner via jobs.user_id).
Logic: if current_user = 'bridgeleads_purge' -> allow. On UPDATE: raise if user_id changed.
`SELECT deletion_state FROM public.users WHERE id = NEW.user_id FOR KEY SHARE`; raise
(SQLSTATE BLD20) unless found and state IS NULL or 'pending'.
Billing ledgers (contact_lookup_*, skip_trace_meter_events, referral_events) are NOT
fenced: late billing evidence from in-flight work is kept, and they hold no PII.

### 2. Functions (SECURITY DEFINER, owner bridgeleads_purge, search_path fixed,
REVOKE EXECUTE FROM PUBLIC/anon/authenticated/service_role/app; GRANT to bridgeleads_system)
- `claim_account_deletion(p_lease interval)` RETURNS (deletion_id, user_id, claim_token,
  reclaimed bool) or no row. Candidate = one account_deletions row (SKIP LOCKED) that is
  either status 'pending' AND purge_after <= now() AND stripe_state IN ('cancel_set',
  'not_applicable') AND (next_attempt_at IS NULL OR <= now()), or status 'purging' AND
  claimed_until < now() (expired lease -> reclaim, rotate token). Then lock the users row
  FOR NO KEY UPDATE, then the deletion row FOR UPDATE (users-first lock order), re-verify,
  transition pending -> purging on both (or rotate token), set claimed_until = now()+lease,
  attempts+1. The Python caller has already checked "no non-terminal job/batch run/skip-trace
  queue" (until day 40) and alerts ops if Stripe is still unconfirmed at day 40.
- `record_deletion_progress(p_deletion_id, p_claim_token, p_phase text, p_error text)`:
  whitelisted phases: 'scheduled_email_sent', 'stripe_cancel_set', 'stripe_not_applicable',
  'stripe_uncancel_set', 'stripe_waiting_for_period_end', 'stripe_customer_deleted',
  'stripe_failed', 'r2_first_sweep', 'final_email_sent', 'tombstoned', 'r2_final_sweep',
  'error' (attempts/last_error/next_attempt_at backoff). Pre-claim phases (scheduled email,
  stripe cancel/uncancel) need no token and are allowed on pending/restored rows; every
  other phase requires status 'purging' + matching token + live lease.
- `purge_account_data(p_deletion_id, p_claim_token, p_cache_keys text[])`: verify row
  purging + token + lease; `SELECT 1 FROM users WHERE id = uid FOR UPDATE` FIRST (waits for
  every in-flight fenced writer); then per the matrix: DELETE (sessions, avatars, pending
  email changes, password history, MFA codes, notifications, record views, list membership,
  job_logs of the user's jobs, batch_runs, dialer_deliveries, skip_trace_cache WHERE
  address_hash = ANY(p_cache_keys), pending_registrations WHERE email_hmac = users.email_hmac);
  INSERT consumed_trial_emails (users.email_hmac, now()+2y) if users.trial_consumed_at IS NOT
  NULL (ON CONFLICT DO NOTHING); SCRUB (scraper_batches, scraper_configs, jobs, results,
  pending_skip_trace_rows, skip_trace_queues, audit_events.detail) per the matrix columns;
  set db_purged_at. Idempotent: a re-run after a crash repeats harmlessly (the marker is set
  in the same transaction as the work, so a committed purge is never redone half-way).
  jobs.export_key / batch_runs.combined_export_key are captured by Python before (for the R2
  delete) and NULLed here only if r2_first_sweep_at is set (otherwise raise).
- `complete_account_deletion(p_deletion_id, p_claim_token)`: requires db_purged_at,
  final_email_sent_at, tombstoned_at, r2_first_sweep_at, r2_final_sweep_at all set and
  r2_final_sweep_at >= r2_first_sweep_at + 24 h; users then row: users.deletion_state
  purging -> deleted, row status purging -> completed, completed_at, claim_token NULL.
  Stripe cleanup continues afterwards via record_deletion_progress (stripe_* phases allowed on
  completed rows too).

### 3. Grants (mirrored in provision_rls_roles.sql)
bridgeleads_purge: SELECT/DELETE on the DELETE tables; SELECT/UPDATE (scrub columns) on the
SCRUB tables; SELECT on users(id, is_active, deletion_state, email_hmac, trial_consumed_at)
and UPDATE(deletion_state); SELECT, DELETE on skip_trace_cache, pending_registrations;
role-targeted RLS policy `<tbl>_purge FOR ALL TO bridgeleads_purge USING (true)` on every
touched table that has RLS enabled. bridgeleads_system: EXECUTE on the four functions.
Tombstone (Fernet email) stays in Python as bridgeleads_system (UPDATE users), then
record_deletion_progress('tombstoned').

### 4. Also in P3a/P3b
Login (and refresh) must refuse purging/deleted accounts cleanly (401), since the fence would
otherwise turn a user_sessions insert into a 500.

### 5. Tests
Fence: writer paused between trigger and commit (purge waits then deletes its row);
post-claim insert raises BLD20; multi-row insert; user_id re-parent raises; job_logs via jobs;
purge role bypass works. Functions: claim pending->purging only when due + stripe ok; reclaim
after expiry rotates token, old token rejected; purge with wrong token / other deletion raises;
matrix end-state (DELETE tables empty for user, SCRUB columns NULL, KEEP ledger counts
unchanged, other tenant byte-identical); complete refuses until all markers + 24 h gap.
Prod-like simulation (non-superuser owner, Supabase default privileges) for upgrade/downgrade.
