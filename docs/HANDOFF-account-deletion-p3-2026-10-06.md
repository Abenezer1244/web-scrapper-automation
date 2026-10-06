# HANDOFF: account deletion P3 (purge) — 2026-10-06

Read this, then `tasks/todo-account-deletion.md` (plan + every Codex round), then
`docs/product/account-deletion-retention-matrix.md` (owner-signed spec) and
`docs/product/account-deletion-m113-design.md` (design consult input). CLAUDE.md rules apply
(Codex every step, no mock code, merge = deploy, ask the owner before merging migrations).

## State

| Phase | State |
|---|---|
| P1 migration 112 (#472) | LIVE, prod-verified read-only |
| P2a routes (#473), P2b gate (#474) | LIVE; `ACCOUNT_DELETION_ENABLED=false` (owner flips it after P5) |
| P3a retention matrix | owner-signed (address+parcel kept, trial HMAC 2 y, Tracerfy -> counsel) |
| P3a migration 113 | **WIP, untested, not reviewed**: branch `feat/account-deletion-p3a` in worktree `C:\Users\Windows\bl-wt\profile-be`, commit `353c25fb` (local only, not pushed) |
| P3b beat purge task, P4 export, P5 frontend | not started |

## Migration 113 design consult (Codex) - already folded in

Users-first lock order in claim (`FOR NO KEY UPDATE OF u SKIP LOCKED`); purge re-verifies after
`users FOR UPDATE`; `db_purged_at` short-circuits re-runs; reclaim respects `next_attempt_at`;
Stripe markers are compare-and-set; trial upsert keeps the longer expiry; purge batched
(`p_batch`, returns true when done) because the largest prod account has 106k results;
'tombstoned' releases the lease and parks the row until first sweep + 24 h.
Deliberately NOT fenced (documented): audit_events (detail re-scrubbed at complete),
pending_registrations (short-lived, deleted at purge), skip_trace_cache (only written at ingest
from fenced pending rows).

## Next steps, in order

1. Tests for 113 (`tests/test_account_deletion_purge.py`), real DB:
   - fence: writer paused between trigger and commit (two sync connections + thread) -> purge
     waits then cleans its row; post-claim insert -> BLD20; multi-row insert; user_id re-parent ->
     BLD21; job_logs via jobs; purge-role writes pass; app/system roles (integration-marked).
   - functions: claim only when due + stripe ok; reclaim rotates token, old token BLD33; progress
     phase rules + CAS; purge end-state per matrix §4 (DELETE tables empty, SCRUB columns NULL,
     KEEP counts unchanged, other tenant identical, pending_registrations gone, trial row);
     batching returns false then true; complete refuses until all markers + 24 h.
   - measure fence cost: insert ~10k results with/without the trigger.
2. Login + refresh must return 401 (not 500) for purging/deleted accounts (the fence would turn
   a user_sessions insert into BLD20). `src/api/routes/auth_helpers/login.py`.
3. Mirror purge grants/policies in `scripts/provision_rls_roles.sql` (+ cutover policies if
   needed); prod-like simulation (see the sim scripts pattern in the plan's P1 log: non-superuser
   owner with CREATEROLE+BYPASSRLS, ADMIN-only purge membership, Supabase default privileges)
   for upgrade/downgrade with zero leaked privileges.
4. Codex diff review to GATE PASS -> PR -> CI -> ask owner before merge -> verify prod read-only.
5. P3b beat task: Stripe cancel/uncancel + scheduled email from `stripe_state` /
   `scheduled_email_sent_at` (pending rows), in-flight precondition until day 40 + ops alert,
   claim -> R2 sweep 1 (prefix + referenced keys, capture keys first) -> purge batches (cache keys
   from `pending_row_subject_key` BEFORE scrubbing) -> final email -> tombstone (Python, Fernet
   placeholder email) -> wait 24 h -> reclaim -> R2 sweep 2 -> complete -> Stripe customer delete
   once the sub has ended. Then P4 export, P5 frontend, then the owner flips the flag.

## Open items for counsel (owner-acknowledged)

Tracerfy deletion path in the vendor contract; California coverage/data-broker status; WA tax
location fields; FCRA prohibited-use clause; downloaded-files policy wording; Supabase backup
window. Homeowner suppression is the NEXT project (and the bridgeleads.com contact address in the
published policies does not receive mail).

## Landmines hit this session

- Codex prompts: always via stdin from the scratchpad, `codex exec -c mcp_servers={}
  --skip-git-repo-check - < prompt.txt`.
- Background waiters get killed on low memory: poll in the foreground in <= 10-minute loops.
- audit_log is fire-and-forget: tests must `await asyncio.gather(*security._audit_tasks)`.
- Download tokens use a whole-second cutoff: a test minting in the same second passes for the
  wrong reason.
- Test env: `source <scratchpad>/env.sh` from the profile session (see the previous handoff);
  `unset DATABASE_URL_MIGRATE; export ENVIRONMENT=test` before alembic.
