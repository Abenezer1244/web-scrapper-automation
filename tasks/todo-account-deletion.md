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
  `POST /auth/account/restore`, `POST /auth/logout`, `POST /auth/refresh`, and (P4)
  downloading an export made before the request. Creating a new export while pending is
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

- [ ] **P1 migration 112 (schema only, no ORM)** `bridgeleads_purge` role (guarded, mirrored
      in provision_rls_roles.sql); `users.deletion_state` + CHECK + state-machine trigger;
      `request_account_deletion` / `restore_account_deletion` definer functions;
      `account_deletions` (+ partial unique, RLS user isolation + system policy, grants per the
      111 pattern); `consumed_trial_emails`. Test: `alembic upgrade head` + constraint tests.
- [ ] **P2 request + restore + gate** ORM mapping; `POST /auth/account/delete` (session +
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
