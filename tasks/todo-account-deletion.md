# Account deletion + data export (profile follow-up 5)

Design + owner decisions: `docs/product/account-deletion-and-export.md` (§4 decided 2026-10-06:
30-day undoable grace, cancel at period end/no refund, retention table §4.1, Stripe §4.4).
Homeowner suppression is the NEXT project, not this one.

## Facts the plan rests on (codebase map, 2026-10-06, main @ 7df7abc7, alembic head 111)

- `users` has no deleted/deactivated column; nothing sets `is_active=False`, but auth already
  rejects inactive users (`src/api/auth.py:327` API key, `:395` JWT).
- `email` is Fernet-encrypted NOT NULL; `email_hmac` NOT NULL UNIQUE, recomputed by
  `@validates("email")`. Tombstone = unique placeholder email (`deleted+<uuid>@invalid`).
- All 23 user FKs are CASCADE (referred_by = SET NULL). We tombstone, so no cascade fires:
  every purge is an explicit statement. `audit_events.user_id` has no FK (survives).
- Workers/beat run as `bridgeleads_system`: SELECT/INSERT/UPDATE everywhere, DELETE only on a
  short list. New DELETE grants go in 3 places + migration mirror (provision_rls_roles.sql,
  `_cutover_step2_grants_policies.py`, `verify_worker_delete_grants.py`; guard test
  `tests/test_worker_delete_grants.py`). `user_sessions`, `user_avatars` have NO system
  policy/grant today.
- R2: every export is under `exports/{user_id}/`; `DataExporter.delete_from_r2` is 404-safe;
  sweep pattern in `retention.py:_sweep_exports` (delete object, then conditional NULL).
- Stripe: server never cancels today (Customer Portal only). `customer.subscription.updated`
  already maps `cancel_at_period_end` -> `entitlement_ends_at`; `.deleted` -> `end_subscription`.
- Schedules: `scraper_configs.active` + `paused_reason` ('entitlement' = system-paused,
  revived by `apply_reconciliation_*`). New reason 'account_deletion' fits.
- Revocation in-txn pattern: `revoked_at=now; api_key_hash=None; update_revoke_cache`, 503 on
  RedisError (`email_change.py:190-205`).
- Schema-first: migration PR ships alone (no ORM mapping); tests run `alembic upgrade head`.

## Design choices (revised after Codex plan round 1, see Review log)

- **Lifecycle state, DB-fenced.** `users.deletion_state` NULL | `pending` | `purging` | `deleted`
  (CHECK). The gate and the purge read it; it is the fence, not the API check.
- **`account_deletions` table** (one row per request = operation id): status
  `pending|purging|completed|restored`, `requested_at`, `purge_after` (= requested + 30 d,
  UTC; CCPA's 45 d from request is always later), `claimed_until` (lease), `attempts` int,
  `last_error`, `scheduled_email_sent_at`, `final_email_sent_at`, `stripe_state`,
  `completed_at`, `restored_at`. Partial UNIQUE: one open (pending/purging) row per user.
  Repeated request while pending = idempotent (same row, deadline NOT extended).
- **Restore** only `WHERE deletion_state='pending'` -> else 409 `deletion_already_started`.
- **Purge claim** atomic `pending -> purging` (users + row, one txn, `claim_token` uuid +
  `claimed_until` lease + `next_attempt_at`), committed BEFORE any R2/DB/Stripe/email work.
  Deferred (not failed) while the user has a non-terminal job, batch run or skip-trace queue,
  but only until day 40 (`purge_after` + 10 d): after that the purge proceeds under the
  fence (late writers raise; R2 sweep 2 catches late uploads), keeping completion inside
  CCPA's 45 days.
- **DB write fence (rounds 2-3).** A `BEFORE INSERT OR UPDATE … FOR EACH ROW` trigger
  (`ENABLE ALWAYS`, on every partition) on every purge-target table runs
  `SELECT deletion_state FROM public.users WHERE id = NEW.user_id FOR KEY SHARE` and raises
  unless the row exists and the state is NULL or `pending`. KEY SHARE (not FOR UPDATE) so
  writers for one user do not serialize; it still conflicts with the purge's FOR UPDATE.
  The purge takes `SELECT … FROM public.users WHERE id=$1 FOR UPDATE` as its own first
  statement, before any DELETE: it waits for every writer that already passed the trigger,
  and a writer arriving later blocks in the trigger, then re-reads the row under READ
  COMMITTED, sees `purging` and raises. The trigger also makes `user_id` immutable on
  UPDATE (no row can be re-parented out of a purge), and lets the write through when
  `current_user = 'bridgeleads_purge'` (only the purge definer functions run as that role:
  this is the trusted path for the retained-PII scrub). Non-FK child UPDATEs hold row locks the purge
  DELETE waits on. P3a audits every purge target for a direct `user_id` (indirect children
  get a trigger that resolves and locks the owning user) and for DEFERRABLE FKs (the trigger
  lock is the fence, not the FK).
- Fence and state-machine trigger functions are explicitly `SECURITY INVOKER` (so a trigger
  fired inside a definer function sees `current_user = bridgeleads_purge`).
- **Lifecycle transitions only through definer functions** owned by `bridgeleads_purge`:
  `request_account_deletion`, `restore_account_deletion` (EXECUTE: app role),
  `claim_account_deletion` (atomic reclaim of an expired lease rotates `claim_token`),
  `mark_deletion_phase`, `complete_account_deletion` (EXECUTE: worker role). Each locks and
  validates the row, user, token/lease and required phase markers. A state-machine trigger on
  `users.deletion_state` and `account_deletions.status` allows only NULL->pending,
  pending->NULL, pending->purging, purging->deleted, AND raises on any change unless
  `current_user = 'bridgeleads_purge'`, so the broad table UPDATE grants of app/worker roles
  cannot move the lifecycle even through a valid transition. Every lifecycle function has
  `REVOKE EXECUTE … FROM PUBLIC` then an explicit grant. Request/restore take NO user id
  parameter: they read `app.current_user_id` (the GUC the API binds from the authenticated
  session), so a caller can only act on itself. App/worker roles get no INSERT/UPDATE/
  DELETE on `account_deletions` at all (only the purge role writes it). Lock order everywhere: `account_deletions` row,
  then `users` row; deadlocks retried. Retained tables are not trigger-fenced (late billing evidence is
  kept); any retained PII column gets the trigger too (decided in the P3a matrix).
- **R2 fence.** Sweep once after the claim and again >= 24 h later (longer than any Celery
  hard time limit), marker-based pagination until the prefix lists empty; completion only
  after the second sweep confirms empty (fail closed).
- **Phase markers** on `account_deletions`: `db_purged_at`, `final_email_sent_at`,
  `tombstoned_at`, `r2_first_sweep_at`, `r2_final_sweep_at`, `stripe_state`; every phase
  checks its own marker, so a retry resumes. `users.deletion_state` stays `purging` until
  every marker is set, then `deleted` (same txn as row `completed`).
- **Order:** claim -> R2 sweep 1 -> DB purge (fn) -> final email (recipient still on the
  row) -> tombstone (original email HMAC captured first for the trial table) -> >= 24 h ->
  R2 sweep 2 -> complete (`deletion_state='deleted'`). Stripe cleanup is reconciled after
  completion (`stripe_state='waiting_for_period_end'` -> deleted Customer) and does not
  hold local deletion open.
- **Purge deletes via one SECURITY DEFINER function** `purge_account_data(deletion_id,
  claim_token)` (the only signature; no uid overload) owned by a dedicated
  `bridgeleads_purge` role: NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE
  NOINHERIT, granted to NO role (verified recursively), owns no tables (so RLS applies to
  it without FORCE; ownership test), app/worker roles are not superuser/CREATEROLE;
  database superusers are trusted and out of scope; exact DELETE/SELECT/UPDATE grants + role-targeted RLS policies on the purge and
  scrub tables plus `users` and `account_deletions` (NOBYPASSRLS, so RLS applies to it
  and every touched table needs a role-targeted policy), created idempotently in the migration (cluster-wide role, guarded)
  and in provision_rls_roles.sql; `SET search_path = pg_catalog, pg_temp`,
  every object schema-qualified; `REVOKE EXECUTE … FROM PUBLIC`, EXECUTE granted only to
  `bridgeleads_system`. It locks the `account_deletions` row and its `users` row, and raises
  unless status/state are `purging`, the token matches and the lease is live; all deletes
  use that verified user id. RLS applies (owner is NOBYPASSRLS) and the explicit
  checks are the primary boundary. It also scrubs third-party PII columns in retained
  tables. Tests: PUBLIC/app role cannot execute; wrong token, expired lease, other user's
  deletion_id all raise; worker cannot SET ROLE bridgeleads_purge. Tombstone (Fernet email)
  stays in Python: placeholder `deleted+<uuid>@invalid` via ORM so the validator recomputes
  `email_hmac`; `is_active=false` (column exists, auth already rejects it),
  names/timezone/MFA/password_hash scrubbed. "Deleted" = `deletion_state='deleted'`
  (no separate `deleted_at`).
- **External side effects never share the DB transaction.** The lifecycle change and the
  pending-work markers commit first; Stripe calls and emails run after, record their result
  idempotently (`stripe_state`, `*_sent_at`), and the beat task re-drives anything still
  pending, including "Stripe succeeded but our DB write after it failed".
- **Stripe desired state, reconciled.** pending -> `cancel_at_period_end=true`; restored ->
  un-cancel if still in period. Tried inline in the route (idempotency key) AND re-applied by
  the beat task until `stripe_state` confirms. The Stripe Customer is deleted only after the
  subscription has ended and no invoice is open; if that is later than day 30, local data
  is purged on time and Stripe cleanup retries (`stripe_state`). Webhooks map by Stripe ids
  only; they must never move `deletion_state` back; reconciliation must never revive
  `paused_reason='account_deletion'` (test).
- **403 gate allowlist (exhaustive):** `GET /auth/me` (shows state + date),
  `POST /auth/account/restore`, `POST /auth/logout`, `POST /auth/refresh`. (P4 decision A,
  2026-10-07: no export download while pending; restore first.) Creating a new export while pending is
  403 (the dialog says "download your data first"). Everything else 403 `account_pending_deletion`. API keys are cleared on
  day 0 so they cannot reach the gate.
- **Trial farming:** at purge, if `trial_consumed_at` was set, keep the email HMAC in
  `consumed_trial_emails` (fraud exception, documented, 2-year expiry); registration
  checks it.
- **Emails:** at-least-once with sent-at columns (rare duplicate on crash accepted); Stripe
  calls use idempotency keys `acctdel-<deletion_id>-<action>`.
- **Webhooks:** for `deletion_state` `purging|deleted`, Stripe handlers no-op (logged); for
  `pending` they run normally but can never change `deletion_state` or revive
  `paused_reason='account_deletion'`.
- **Retained** (not purged): billing ledgers, `delivered_records`, `audit_events`. P3 starts
  with a column-level retention matrix (incl. JSON/detail columns, vendor phones/emails in
  retained tables, `skip_trace_cache` keyed by hash not user_id, Redis keys, Celery results,
  logs, backups/PITR) as its acceptance criterion.
- Expiry jobs for retention periods (12/24 months, 7 years) are a later phase.

## Phases (each: plan -> Codex -> build -> tests -> Codex diff review to GATE PASS -> PR -> CI -> merge -> verify; owner approval between phases)

- [x] **P1 migration 112 (schema only, no ORM)** `bridgeleads_purge` role (guarded, mirrored
      in provision_rls_roles.sql); `users.deletion_state` + CHECK + state-machine trigger;
      `request_account_deletion` / `restore_account_deletion` definer functions;
      `account_deletions` (+ partial unique, RLS user isolation + system policy, grants per the
      111 pattern); `consumed_trial_emails`. Test: `alembic upgrade head` + constraint tests.
- [ ] **P2a request + restore (design rev 2 after Codex consult)**
      Both routes behind `settings.ACCOUNT_DELETION_ENABLED` (default False -> 404): nobody can
      open a deletion that nothing will finish until the P3 beat is live and verified.
      `POST /auth/account/delete` {current_password, mfa_code?, confirm_email}: require_session +
      get_rls_db; `_reauthenticate`; MFA (`_consume_second_factor` + MfaFailureGuard); then ONE
      transaction: `request_account_deletion()` FIRST (it locks the users row; BLD01 -> 409),
      then compare confirm_email (normalize_email + blind_index) with the locked row (mismatch
      -> rollback, 400). `created=false` is a pure no-op (no pause/revoke/audit). On create:
      pause configs (active OR paused_reason='entitlement' -> active=false,
      paused_reason='account_deletion'; user-paused ones stay as they are), sign out everywhere
      in-txn (revoked_at, api_key_hash=None, live user_sessions rows revoked,
      update_revoke_cache; RedisError -> rollback + 503), commit, audit
      `account_deletion_requested`. 200 {purge_after}.
      `POST /auth/account/restore` {current_password, mfa_code?}: same step-up as delete;
      `restore_account_deletion()` (BLD02 -> 404, BLD01 -> 409); configs stay paused; audit
      `account_deletion_restored` only on a real transition.
      No email and no Stripe call from the routes: the P3 beat sends the "scheduled" email
      (`scheduled_email_sent_at` marker, only while status is pending) and drives
      cancel/uncancel from `stripe_state`. Restore notice email dropped (anyone who can
      restore can already sign in). Audit stays post-commit like every other security event.
      Files: models.py, schemas.py, auth_helpers/account_deletion.py (new), routes/auth.py,
      config/settings.py + .env.example (+ tests, openapi).
- [ ] **P2b gate** 403 `account_pending_deletion` in get_auth_context (allowlist GET /auth/me,
      POST /auth/account/restore; logout/refresh never pass through it), `/auth/me` gains
      deletion_state + purge date, dispatcher `quota_block_reason` refuses pending users.
- [ ] ~~**P2 request + restore + gate**~~ (split into P2a/P2b above) ORM mapping; `POST /auth/account/delete` (session +
      password + TOTP if enabled + typed email; idempotent); `POST /auth/account/restore`;
      day-0 effects in one txn (pause configs `paused_reason='account_deletion'`, revoke
      sessions + API key per the email_change pattern, 503 on Redis failure); inline Stripe
      cancel; scheduled email; audit events; the 403 gate; dispatcher skips non-NULL state.
      Tests: idempotency, restore 409 once purging, gate allowlist, reconciliation does not
      revive, Stripe failure leaves stripe_state pending.
- [ ] **P3a retention matrix + migration 113** column-level matrix + FK/trigger closure
      audit (doc; acceptance gate), then fence triggers, claim / phase / complete functions,
      `purge_account_data(deletion_id, claim_token)` + EXECUTE grant
      (mirrored in provision_rls_roles.sql). Concurrency tests: writer paused between trigger
      and FK, multi-row/COPY insert, non-FK update, direct state-column update by app/worker
      roles, worker role privilege boundaries, `SET ROLE` / `SET SESSION AUTHORIZATION
      bridgeleads_purge` fails for app and worker roles, cross-tenant negative, user_id re-parenting
      rejected, expired-lease reclaim, scrub through the trusted path, no app/worker role can
      `DELETE FROM users` (CASCADE would be a second purge path). Plus an inventory of every
      R2 writer and Celery hard time limit (the 24 h second sweep must exceed all of them).
- [ ] **P3 contract (from P2a consult):** Stripe calls outside DB txns with idempotency keys,
      ambiguous timeouts re-driven; the purge does NOT start until `stripe_state` is
      `cancel_set` or `not_applicable` (a deleted account must never keep being charged),
      ops alert if still unconfirmed at day 40; the scheduled email is sent by the beat
      only while status is pending; then flip ACCOUNT_DELETION_ENABLED.
- [ ] **P3b purge beat task** claim -> in-flight precondition -> R2 sweep 1 (paginated list
      helper) -> `purge_account_data` (deletes + retained-PII scrub) -> final email ->
      tombstone + trial HMAC -> >= 24 h -> R2 sweep 2 (fail closed) -> Stripe cleanup when
      -> complete; Stripe cleanup reconciled afterwards; Stripe cleanup and
      desired-state reconcile loop; lease, attempts, backoff, ops alert. Fault-injection test
      after each step; restore-vs-claim race test.
- [ ] **P4 export** `POST /auth/export` (session + password, 1 per 24 h) -> worker ZIP under
      `exports/{user_id}/account/`, DataExporter (CSV-sanitised), 7-day signed link.
- [ ] **P5 frontend** Settings > Account > Your data: export, delete dialog (period-end
      billing, typed email, password/TOTP), grace banner + Restore.
- [ ] Later: retention expiry jobs; homeowner suppression project.

## Review log

- Plan round 1 (Codex): PLAN: REVISE. P0 fence/claim (adopted: lifecycle state + atomic claim +
  in-flight precondition; per-write re-check rejected as too broad), P0 Stripe ordering
  (adopted: desired-state reconcile, delete Customer only after sub ended), P0 RLS/grants
  (adopted via SECURITY DEFINER function instead of broad grants), P1 outbox under-specified
  (adopted: account_deletions table), P1 retention matrix, P1 tombstone, P1 gate allowlist,
  P1 webhook races, P1 R2 paginated sweep, P2 idempotency/fault tests: all adopted.
  Open Qs: trial farming -> keep HMAC (adopted); duplicate email -> sent-at, at-least-once.
- Plan round 2 (Codex): PLAN: REVISE. P0 fence still observational -> adopted a real DB fence
  (row triggers + purge FOR UPDATE vs FK KEY SHARE) instead of a per-writer lease protocol;
  P0 SECURITY DEFINER hardening -> adopted (search_path, PUBLIC revoke, deletion_id+token
  signature, explicit checks), dedicated owner role argued against. P1 phase markers, email
  before tombstone, gate export rule, webhook terminal no-op, R2 fail-closed, HMAC capture:
  adopted.
- Plan round 3 (Codex): PLAN: REVISE. P0 trigger lookup was a plain SELECT (race before the
  FK lock) -> trigger now locks FOR KEY SHARE (Codex said FOR UPDATE; KEY SHARE closes the
  race without serialising a user's writers). P0 indirect/deferred FKs -> P3a audit gate.
  P0 state columns writable by worker -> state-machine triggers. P0 trigger bypass -> ENABLE
  ALWAYS (replica mode needs superuser; superuser out of scope). Dedicated owner role:
  Codex not persuaded, adopted (`bridgeleads_purge`). P1 signature/order/lock order/
  deleted_at/is_active: fixed.
- Plan round 4 (Codex): PLAN: REVISE. KEY SHARE deviation confirmed correct. P0 lifecycle
  writable by broad roles -> transitions only via definer functions + trigger requires
  current_user = bridgeleads_purge. P0 re-parenting escapes fence -> user_id immutable.
  P0 scrub vs fence -> same trusted-role exception. P1 lease reclaim, R2 writer inventory,
  Stripe after completion; P2 RLS policies for purge role, no DELETE users: adopted.
- Plan round 5 (Codex): PLAN: REVISE. current_user semantics confirmed. P0 role hardening
  (full attribute list, no membership, SECURITY INVOKER triggers, SET ROLE/SESSION AUTH
  tests, superuser trusted), P1 RLS vs ownership, P1 phase order (role + request/restore
  functions moved into P1): adopted.
- Plan round 6 (Codex): no P0. P1 definer boundary (PUBLIC revoke, caller-bound via GUC, no
  direct writes to account_deletions), P1 bounded drain (day 40), P1 external side effects
  after commit + reconcile: adopted.
- P1 build (migration 112) Codex diff review, 6 rounds -> GATE PASS. Adopted: non-superuser
  downgrade drops the functions as their owner; account_deletions id/requested_at immutable;
  DROP POLICY IF EXISTS before CREATE; app SELECT policy mirrored in provisioning; purge-role
  membership locked to the migration owner's inert ADMIN row (hand-over REVOKEs its own
  grant); no anon/authenticated/service_role DELETE/TRUNCATE on users (CASCADE into
  account_deletions). Refuted with evidence: restore's row lock needing table-wide UPDATE
  (test runs it as the purge role), DELETE-users cascade by runtime roles (no grant;
  asserted), provisioning roles absent (the script creates them first). Accepted residual
  (Codex agreed, not P1): the GUC is set by the shared API role, the same trust boundary as
  every RLS policy; the DB has no per-end-user principal.
  Verified: local PG16 superuser round trip; prod-like PG16 simulation (non-superuser owner
  with CREATEROLE+BYPASSRLS, ADMIN-only purge membership, Supabase default privileges)
  upgrade -> downgrade -> upgrade with zero leaked privileges (check proven to detect a leak).
- P2a design consult (Codex): DESIGN: REVISE. Adopted: created=false pure no-op; readiness flag;
  restore step-up; email checked under the row lock; scheduled email via the P3 beat outbox;
  purge waits for Stripe cancel confirmation. Not adopted: audit inside the txn (every
  security event in the codebase is audited post-commit). Dropped: restore notice email.
- P2a build Codex diff review, 3 rounds -> GATE PASS. Adopted: lock + re-read the users row
  (FOR NO KEY UPDATE) before the password/second-factor check; API-key mint made a conditional
  UPDATE ... WHERE deletion_state IS NULL (a mint blocked on the deletion's lock could
  otherwise write a fresh key after it); tests for refresh death, repeat no-op, restore with
  flag off, restore MFA, no mint into a pending account. Refuted: missing rate limit
  (_reauthenticate is per-account limited), missing normalization (blind_index normalizes).
  Accepted (existing patterns): Redis cutoff before commit fails safe; MFA guard clear.
- P3a retention matrix (docs/product/account-deletion-retention-matrix.md): Codex 2 rounds; owner
  signed off 2026-10-06 (address+parcel kept, trial HMAC 2 y, Tracerfy to counsel, flag flipped by
  owner after P5). Key finding: billing ledgers CASCADE from jobs/results/configs/batches, so those
  become scrubbed skeletons, not deleted rows. Next: migration 113 (fence triggers + claim/phase/
  complete/purge functions) per the matrix.
- P3a migration 113 build (2026-10-06, session 2). Re-read: claim now sets `is_active=false`
  (every sign-in/refresh/password-reset/API-key lookup already filters it -> 401, no route
  change); repeated purge calls no longer rewrite scrubbed rows. A code audit then found
  cross-tenant beat UPDATEs (skip-trace dispatcher, NTS matcher, dialer push, quota and
  owner/mailing recovery sweeps) that a raise-on-UPDATE fence would abort for every tenant
  once any account is purging. Codex design consult: DESIGN: REVISE. **Owner chose "pin":**
  UPDATE never raises; the matrix SCRUB columns keep their OLD values for a purging/deleted
  owner; INSERT still raises BLD20 under FOR KEY SHARE. UPDATE takes no users lock (would
  deadlock with code holding users FOR UPDATE): its row lock orders it against the purge, the
  purge is re-runnable, and complete refuses (BLD36) while scrubbed data reappeared.
  Adopted from the consult: definer owner-state lookup (no fail-open under the caller's RLS),
  `zz_` trigger name (fires last), skip_trace_queues off the fence (shared Tracerfy batches,
  user_id = first tenant), claim withdraws unsent lookups (dispatcher's own cancel path).
  Accepted residuals (owner): skip_trace_cache rows from an ingest still in flight at a
  forced day-40 purge; per-row deadlock between a child UPDATE and the purge is resolved by
  Postgres (P3b retries 40P01). P3b must: re-run the purge after the 24 h reclaim; treat
  claimed/submitted pending rows as in-flight work in the precondition; dispatcher re-checks
  deletion_state before submitting.
  Prod-like simulation found the migration owner's EXECUTE on the lookup was dropped by
  ALTER OWNER -> granted by the new owner during the hand-over. Fence cost (10k rows):
  INSERT +19%, UPDATE ~77 us/row for an active owner.
  Codex diff review round 1: FAIL. Adopted: audit_events fence (detail never stored for a
  purging/deleted owner), explicit completed/errored queue allowlist. Refuted: trial upsert
  privileges (112 grants INSERT), pending_registrations recreation (registration stages a row
  only when no users row has the HMAC). Accepted P2: only results/list membership batched.
  Round 2: FAIL. Adopted: audit_events.user_id immutable; queues that carried the user's rows
  (via kept pending rows' tracerfy_queue_id) are scrubbed when finished and checked by
  complete. Refuted: lock-taking lookup callable by app/system (they already hold UPDATE on
  users with RLS USING true and can lock any row; every FK insert takes the same lock).
  Round 3: FAIL. Adopted: claim takes users FOR UPDATE SKIP LOCKED (skips an account with
  a write in flight; mutation-checked), complete FOR UPDATE, audit fence locks. Refuted:
  Supabase API roles losing fenced writes (unused; fail closed is intended).
  Round 4: FAIL. Adopted: complete checks scrubbed configs/batches. Refuted: dispatch
  status transitions (all conditional on status='queued'; claimed ones = P3b precondition).
  Round 5: FAIL. Adopted: tombstone invariant checked by the 'tombstoned' marker and complete
  (what SQL can see; email verified in P3b Python); complete refuses while a linked batch
  is pending. Refuted: UPDATE in flight across the >= 24 h gap.
  Round 6: FAIL. Adopted: DELETE/TRUNCATE on the four skeleton tables revoked from the
  Supabase API roles (runtime roles never had it; asserted).
  Round 7: FAIL. Adopted: non-raising skip_trace_queues trigger stores no link/error once
  any tenant of the batch is purging. Refuted: NULL-unsafe compares (columns NOT NULL).
  Round 8: FAIL. Adopted: pending_skip_trace_rows.tracerfy_queue_id pinned.
  Round 9: FAIL. Adopted: queue user_id/tracerfy_queue_id immutable (BLD21; no code
  changes them).
  Round 10: FAIL. Taint lookup made VOLATILE + waiting-webhook race test; mutation showed
  STABLE was not exploitable (called from the volatile trigger).
  **Round 11: GATE: PASS.** Prod-like simulation PASS after every round.

## P3b plan (2026-10-06, draft for Codex consult + owner OK)

Two PRs, each <= 5 files, Python only (no migration). One beat task
`src.workers.scheduler.drive_account_deletions` every 5 min (float, < 10 min rule), thin
wrapper over `_drive_account_deletions_impl()` in a new `src/workers/account_deletion_beat.py`
(added to `src/workers/__init__.py` include). Runs regardless of ACCOUNT_DELETION_ENABLED
(prod has 0 rows; it must already run when the owner flips the flag). External calls never
inside a DB transaction; every phase records its result through record_deletion_progress.
Stripe and R2 go through injectable callables (precedent: `_expire_trials_impl(subscription_lookup=)`),
Resend through the existing fake-module test pattern.

### P3b-1: billing, notice, dispatcher gate
- [ ] `skip_trace_claim.py`: `deletion_state` in ACCESS_COLUMNS; non-NULL -> ACCESS_ENDED, so
      claim, dispatcher filter and the locked re-check all withdraw a pending/purging account's
      lookups (pending users already cannot start jobs: quota_block_reason).
- [ ] Stripe reconcile: pending rows `pending_cancel` -> `Subscription.modify(sub,
      cancel_at_period_end=True, idempotency_key="acctdel-<id>-cancel")` -> CAS to `cancel_set`;
      no subscription / already canceled -> `not_applicable`. Restored rows `pending_uncancel` ->
      modify(False, key "acctdel-<id>-uncancel") only while the sub is still live and set to
      cancel, else `not_applicable`. Failure -> 'error' (no token) backoff; bracket access only
      (stripe 15 StripeObject).
- [ ] Scheduled email (pending, `scheduled_email_sent_at` NULL): date + Restore CTA, then
      record 'scheduled_email_sent' (at-least-once).
- [ ] Day-40 ops alert (`purge_after + 10 d` passed, still not purging): send_ops_alert
      (6 h cooldown), no PII.
- [ ] Tests (real DB): CAS paths, restored uncancel, no-sub, Stripe failure backoff, email once,
      dispatcher withdraws a pending account's queued lookups.

### P3b-2: the purge driver
- [ ] Precondition before the claim: for each due pending row with in-flight work (non-terminal
      job, batch run, skip-trace queue, pending row claimed/submitting/submitted) and not past day
      40 -> 'error' (no token) "waiting for in-flight work" (claim honours next_attempt_at).
- [ ] Claim (lease 15 min) -> by markers, the next phase:
      1. R2 sweep 1: capture jobs.export_key + batch_runs.combined_export_key, list
         `exports/{uid}/` (new paginated `DataExporter.list_r2_keys(prefix)`, native API cursor),
         delete all (404-safe) -> 'r2_first_sweep'.
      2. Cache keys = pending_row_subject_key(row) for every pending row (before the scrub) ->
         purge_account_data batches of 5000 until true (each its own txn; stop if the lease is
         near its end).
      3. Final email to the original address (ORM-decrypted before the tombstone) ->
         'final_email_sent'.
      4. Tombstone via ORM (email `deleted+<id>@invalid` recomputes email_hmac; names/timezone
         NULL; prefs {}; password hash_password(token_urlsafe(32)); MFA/api key/referral cleared;
         is_admin false) -> 'tombstoned' (DB checks the invariant).
      5. After the 24 h reclaim: purge re-run, R2 sweep 2 (delete, then list must be empty: fail
         closed) -> 'r2_final_sweep' -> complete (BLD36 -> error backoff, re-run next tick).
- [ ] Stripe after completion: completed rows with `cancel_set`/`not_applicable` and a customer
      id: Customer.delete only once no live subscription and no open invoice -> CAS
      `customer_deleted`; else wait.
- [ ] Any exception (incl. 40P01) -> 'error' with the token (lease released, backoff).
- [ ] Tests: the whole walk on a real account (matrix §4 end state incl. users row tombstone and
      the address registering again), crash after each phase resumes, two workers never share a
      claim, deferral until day 40, R2 sweep-2 fail-closed.

### P3b Codex design consult (round 1: REVISE) and how each was handled
- Adopted: check the remaining lease before every external call (stop and let the next tick
  resume); Customer.delete re-checks subscriptions + open invoices right before, idempotency key
  `acctdel-<id>-customer`, `resource_missing` = already deleted = success; ops alert also for a
  purging row stuck in retries (attempts >= 6); R2 sweep deletes the captured explicit keys AND the
  prefix, any list error = not swept (fail closed); crash-resume tests around each marker.
- Refuted (evidence): claim cannot reclaim parked rows (it does: tombstoned sets claimed_until=now
  and next_attempt_at=first sweep+24h; tested); ORM tombstone vs the fence (users is not a fenced
  table; its guard only covers deletion_state); ACCESS_ENDED blocks restored accounts (restore sets
  deletion_state NULL); restore tokens in email (none: the CTA is a sign-in link, restore needs the
  password); email changed during grace (pending accounts are 403-gated, email change included);
  preflight race (pending accounts cannot start jobs/batches/lookups: quota gate + P3b-1 dispatcher
  gate; the claim skips accounts with a write in flight).
- Kept by documented decision: proceed at day 40 under the fence (CCPA 45-day deadline, design doc
  "Deferred ... only until day 40"), with an ops alert; emails are at-least-once (Resend 2.7.0 has
  no idempotency key; design: "rare duplicate on crash accepted"). R2 has no object versioning.

## P4 plan: data export (2026-10-07, draft for Codex consult + owner OK)

Spec: design doc §3. P3 is live (main @ 181e2e1c, alembic 114, prod 0 deletion rows, flag OFF).

### Facts this plan rests on (read 2026-10-07)
- R2 presigned URLs 401 in production (`tasks_helpers/status.py:_delivery_download_url`); every
  emailed link is an app URL carrying a revocable `purpose=download` JWT (`download_tokens.py`),
  verified against the jti blacklist + the logout-all cutoff + `is_active` + `deletion_state IS NULL`
  (`jobs.py:_user_from_download_token`). There is no R2 GET helper yet (only PUT/DELETE/list).
- The lead CSV a user "can already download" is built LIVE per job by `GET /jobs/{id}/download`
  (standing rules: actionable, tax cap, category `new`, config layout/hidden fields), only for
  delivered jobs with an `export_key`. Batch combined and segment exports are re-cuts of the same
  rows, so one CSV per downloadable job covers every lead.
- A deletion request already revokes every token (logout-all cutoff, `account_deletion.py`), and
  the P3 R2 sweep deletes everything under `exports/{user_id}/` twice.
- `audit_log` is best-effort fire-and-forget: it cannot hold the 24 h limit. Redis is not durable.
- `retention._sweep_exports(db, stmt, clear_stmt, cutoff, batch)` is generic (delete object, then
  conditional NULL of the key).

### Design
- **Table `account_exports` (migration 115, ask owner first; `purgesim.py` before merge).**
  id uuid PK, user_id uuid NOT NULL FK users, status `pending|building|ready|failed` (CHECK),
  requested_at, claimed_until (lease), attempts, object_key NULL, size_bytes, ready_at,
  expires_at (= ready_at + 7 d), email_sent_at, last_error (no PII). Partial UNIQUE: one
  `pending|building` row per user. RLS: user isolation (app SELECT/INSERT own rows), system
  SELECT/UPDATE (worker); no DELETE for anyone. The 113 fence trigger `account_deletion_fence`
  attached with `object_key, status` pinned (INSERT for a purging/deleted owner raises BLD20; an
  in-flight build can never publish a key after the claim). Retention: KEEP 24 months as the
  request log (CCPA 11 CCR 7101 keeps records of access requests 24 months), matrix row added; the
  purge needs no new grant. Mirrored in provision_rls_roles.sql.
- **`POST /auth/export`** {current_password, mfa_code?}: `require_session` + `get_rls_db`;
  `lock_user` (users row FOR NO KEY UPDATE serialises two clicks); `_reauthenticate`; second
  factor (`_second_factor`); refuse 429 (+ `Retry-After`) if a non-failed export was requested in
  the last 24 h; refuse 409 if one is in progress; INSERT pending row; commit; audit
  `account_export_requested` (added to SECURITY_EVENTS). 202 {id, status}. Pending account = 403
  from the existing gate (not allowlisted). Not behind ACCOUNT_DELETION_ENABLED (export is
  useful alone; owner to confirm).
- **`GET /auth/export`**: the latest export (status, requested_at, ready_at, expires_at,
  next_allowed_at) for the in-app panel. **`GET /auth/export/{id}/url`**: 60 s token URL
  (mirror of `/jobs/{id}/export-url`), 404 unless ready, own and unexpired.
- **`GET /auth/export/{id}/download?token=`**: token-only. New `purpose=account_export` token
  (claim `export_id`, own mint fn in `download_tokens.py`, so a job token can never open an export
  or vice versa); verification shares `_user_from_download_token`'s checks (refactored to take the
  purpose + claim name). Streams the ZIP from R2 via a new `DataExporter.stream_from_r2(key)`
  (native API GET, like list/delete). `Cache-Control: no-store`, export rate-limit zone.
- **Worker: outbox, not `.delay()` alone.** Beat `build_account_exports` every 1 min (own advisory
  lock) claims `pending` rows (lease 30 min, `FOR UPDATE SKIP LOCKED`), and the route also
  `.delay()`s for latency; a lost message is picked up by the beat. Skip + `failed` if the owner's
  `deletion_state` is not NULL (checked at claim and again before upload and email). Build in a
  temp dir:
  - `profile.json` (email, names, timezone, plan, subscription status, created_at, notification
    prefs, mfa_enabled; never hashes, keys or tokens),
  - `scrapers.json` (configs incl. schedule/fields/deliver), `batches.json`, `runs.json`
    (jobs + batch_runs: status, counts, dates; no export keys),
  - `leads/<config-name>_<job8>.csv` per downloadable job, through the SAME query as the job
    download (extracted from `jobs.py` into a shared helper so the two can never drift) and
    `DataExporter.export(fmt="csv")` (CSV-injection sanitised by the shared builder). The JSON
    files are not lead rows (DataExporter's JSON is the lead-row schema), so they are `json.dump`
    of explicit allowlisted dicts; the CSV injection risk does not apply to JSON.
  - Every query filters `user_id` explicitly. ZIP_DEFLATED, uploaded to
    `exports/{user_id}/account/{export_id}.zip` (inside the P3 sweep prefix).
  - Then CAS `building -> ready` with key/size/expiry, then email (7-day token link, capped at
    expires_at) -> `email_sent_at` (at-least-once). Failure -> attempts++, backoff, `failed`
    after 3 (row + ops log, no PII); a failed export does not count toward the 24 h limit.
- **Expiry:** retention gains one `_sweep_exports` call for `account_exports.object_key` with
  `expires_at < now()` (object deleted first, then key NULLed conditionally). Download refuses
  past `expires_at` even before the sweep runs.
- **Size guard:** a hard cap on rows per export (configurable setting) so one huge account cannot
  run a worker out of disk/time; over the cap -> `failed` with a "contact support" message (owner
  to confirm the cap; today's largest account is far below it, to measure read-only).

### Open question (handoff §3): the download belt during the grace period
The design doc says a pending user may download an export made BEFORE the request; the P2b belt
refuses every link for a non-NULL deletion_state. Options:
- **A (recommended): keep the belt strict.** The request already kills every link (logout-all
  cutoff), the delete dialog says "download your data first", and a pending account is gated to
  /auth/me + Restore. A user who forgot restores, downloads, and asks again. Change: amend design
  doc §2 + the allowlist line above. No code.
- B: allowlist `GET /auth/export`, `/url` and the download for pending accounts, only for exports
  with `requested_at <` the deletion's `requested_at`. Tokens are freshly minted after sign-in, so
  revocation holds; cost: a wider gate, and a pending account keeps a working data path for 30 d.

### Codex design consult round 1: DESIGN: REVISE (no P0). Revised design, supersedes the above
- **Table (revised):** + `claim_id` uuid, `next_attempt_at`, status adds `expired`. CHECKs:
  `object_key` is exactly `'exports/'||user_id||'/account/'||id||'.zip'` when set (so no row can
  point at another tenant's object; download/retention also recompute it); `ready` => key, size,
  ready_at, expires_at set; `pending|building` => no key; `expires_at = ready_at + 7 days`. App
  role: column INSERT on `(user_id)` only + RLS WITH CHECK own id, SELECT own rows; no UPDATE.
  Worker (`bridgeleads_system`, not owner, NOBYPASSRLS policy pattern as 114) SELECT/UPDATE.
- **One worker entry point:** the beat only (every minute, no `.delay()` from the route). Claim =
  one conditional UPDATE (`pending`, or `building` with an expired lease, and `next_attempt_at`
  due) `FOR UPDATE SKIP LOCKED`, rotating `claim_id`; every later write matches id + status +
  claim_id + live lease.
- **Finalise vs deletion (race):** the `building -> ready` CAS runs in a txn that first takes
  `SELECT deletion_state FROM users ... FOR SHARE` (conflicts with the deletion request's FOR NO
  KEY UPDATE; users row first, the same lock order as 112/113) and requires it NULL. Refused ->
  the uploaded object is deleted and the row goes `failed` ("account scheduled for deletion").
  So an export is downloadable only if it was ready before any deletion request. Deletion
  supersedes an export in progress; the P5 dialog shows "an export is being prepared".
- **Email separate from build:** `ready` never reverts on a Resend failure; email retried on its
  own (`email_sent_at`, backoff), sent to the account's CURRENT address read at send time.
- **Redaction allowlists for every JSON file** (seeded-secret test): `deliver` keeps only
  non-secret keys (method, layout, destination type); `webhook_secret`, `dialer_webhook_secret`,
  API keys, headers and tokens never leave. `last_error` is a fixed code, never exception text.
- **ZIP member names from ids only** (`leads/<job_id>.csv`; `runs.json` maps job -> config
  name/county/type), so no user string reaches a path.
- **Limits:** row cap, ZIP byte cap (checked before upload), Celery soft time limit; temp dir
  removed in `finally`; nothing published until the ZIP is complete.
- **Snapshot:** all reads in one REPEATABLE READ read-only transaction (build-time snapshot).
- **CSV injection:** already an invariant: `write_lead_csv` runs every cell through
  `sanitize_for_csv` (`lead_export.py`); test formula payloads in every lead field.
- **Order of refusals on POST:** 403 pending (gate) -> step-up -> 409 in progress -> 429 within
  24 h. URL minting rate-limited (export zone), separate from download.
- Refuted: CSRF (the API authenticates by `Authorization: Bearer`, never cookies). Kept by
  existing pattern: token in the query string (same as every emailed job link; no-store,
  `Referrer-Policy: strict-origin-when-cross-origin`, the ZIP is an attachment with no page
  resources; the token is never logged by our code), audit best-effort (quota lives in the DB).
- **A vs B:** Codex recommends A (keep the belt strict).

### Owner decisions (2026-10-07)
1. A: belt stays strict; design doc §3 amended. 2. Migration 115 approved; rows KEEP 24 mo
(request log, matrix row). 3. Own flag `ACCOUNT_EXPORT_ENABLED` (default false; 404 when off).
4. Caps: 250k lead rows, 200 MB ZIP (settings), largest real account measured read-only first.

### P4a build (migration 115) review log
- Built as revised above, except: the ZIP key is not stored at all (derived from user_id + id), and
  the fence is attached with no pinned columns (publishing is ordered by the worker's users FOR SHARE;
  a pin would make the expiry sweep retry a deleted owner's row forever).
- Codex diff review round 1: FAIL. Adopted: expiry needs ready_at (NULL hole), non-negative counters,
  constrained last_error. Refuted: CASCADE FK defeats retention (no role can delete users, asserted since
  112; account_deletions has the same FK), provisioning role guards (the script creates the roles
  first), role tests skipping in CI (existing pattern; purgesim enforces the matrix).
- Round 2: FAIL (shape check admits names) -> fixed allowlist of codes. **Round 3: GATE: PASS.**
- purgesim.py extended (account_exports role x privilege matrix + column grants, proven to catch a
  hand-granted DELETE): PASS after every round. 21 fenced tables ENABLE ALWAYS.
- Merged #480 (owner OK) as c0fc56ec; prod verified read-only (alembic 115, grants, policies, fence,
  CHECKs, 0 rows); VERIFIED posted.

### P4b build (worker) review log
- Shared: `download_rows_select()` (results_category.py) and `config_export_options()` (lead_export.py),
  extracted from GET /jobs/{id}/download, which now uses them (475 download/export/beat tests pass).
  No ORM model (account_deletions has none either). Expiry runs in the export beat, not retention
  (RETENTION_PURGE ships off). Configs exported through ScraperConfigResponse (secrets already
  write-only there); batch deliver filtered by DELIVER_SECRET_FIELDS. Verified the build session is
  REPEATABLE READ + read-only with the GUC bound. Largest prod account: ~91k raw rows / 100 runs.
- Mutation-checked: FOR SHARE re-check, secret filter, lease check, email deletion filter, row cap.
- Codex round 1: FAIL. Adopted: LIMIT remaining+1 per job, rows expunged per job, ZIP size checked as
  it grows; claim only attempts < 3 + separate give-up; local temp-file sweep. Refuted: email race
  (a deletion request and an email change both raise the logout-all cutoff in the same txn, so a link
  minted before either is dead; no lock across an external call).
- Round 2: FAIL (give-up marked failed before deleting the object) -> delete first, then CAS; a failed
  delete is retried next tick (failure-injection test). **Round 3: GATE: PASS.**

### PRs (each: tests on the real DB, stand-ins only for R2/Resend; Codex diff review to GATE PASS)
- [ ] **P4a migration 115** (alone, schema-first): table, CHECK, partial unique, RLS + grants,
      fence trigger, provisioning mirror, downgrade; tests (constraints, RLS cross-tenant, fence
      BLD20 + pin, no DELETE); `purgesim.py` extended + PASS. Owner OK before merge.
- [ ] **P4b worker**: ORM model, shared deliverable-rows query (jobs.py refactor, behaviour
      unchanged), `stream_from_r2`, builder + beat task + email, retention sweep line. Tests: full
      ZIP content on a seeded account (another tenant's rows absent), CSV injection sanitised,
      pending account refused, crash after upload resumes, lease, expiry sweep.
- [ ] **P4c routes**: POST/GET export, url, download, token purpose, schemas, OpenAPI regen (0
      deletions) -> FE types-regen PR. Tests: step-up + MFA, 24 h limit, 409 in progress, 403
      pending, cross-tenant 404, token purpose/claim/expiry/logout-all/deletion belt, audit event.
