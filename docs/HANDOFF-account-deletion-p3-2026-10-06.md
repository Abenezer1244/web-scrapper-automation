# HANDOFF: account deletion, phase P3 (the purge) — 2026-10-06

Read this whole file first. Then follow CLAUDE.md:
- Codex in the loop on every step: design consult before code, diff review to GATE PASS.
- Security baseline applies.
- No mock code.
- Merge = deploy.
- **Ask the owner before merging any migration.**

## 1. The goal

The owner asked for the 5 profile & account follow-ups to be finished one by one, with Codex in the loop. Items 1-4 are done and live. **Item 5 is "Delete my account + Export my data"**, built in phases:

| Phase | What | State |
|---|---|---|
| P1 | migration 112: lifecycle schema, NOLOGIN role `bridgeleads_purge`, request/restore functions | **LIVE** (BE #472) |
| P2a | `POST /auth/account/delete` + `/auth/account/restore`, behind `ACCOUNT_DELETION_ENABLED` | **LIVE** (BE #473), flag OFF |
| P2b | 403 gate for pending accounts (only `GET /auth/me` + restore allowed), /auth/me deletion fields, worker `quota_block_reason` refusal, download-link belt | **LIVE** (BE #474) |
| P3a | retention matrix (owner-signed) + **migration 113** (write fence + claim/progress/purge/complete functions) | matrix done; **113 is WIP: untested, unreviewed, local only** |
| P3b | beat purge task (Stripe, emails, R2 sweeps, purge batches, tombstone, complete) | not started |
| P4 | `POST /auth/export` (ZIP of the user's data, 7-day link) | not started |
| P5 | frontend: Settings > Account > Your data (export, delete dialog, grace banner + Restore) | not started |
| — | owner flips `ACCOUNT_DELETION_ENABLED=true` | only after P3, P4 and P5 are live and verified |

### Owner decisions (2026-10-06, all final)

- **Grace period:** 30 days, which the user can undo by signing in and pressing Restore.
- **Billing:** cancel at period end, no refund.
- **Retention:**
  - billing records: 7 years;
  - high-value audit events: 24 months;
  - routine audit events: 12 months;
  - `delivered_records`: 24 months.
- **Stripe:** keep the Customer through the grace period and the final invoice. Delete it only after the subscription has ended. Never use Stripe redaction: invoices can't be redacted, and redacted charges can't be refunded and lose disputes.
- **Lead rows:** keep property address and parcel (public record). Blank everything else personal.
- **Trial abuse:** keep a 2-year email HMAC of deleted accounts that used the trial, in `consumed_trial_emails`.
- **Tracerfy copies:** no vendor call in the purge; this is a counsel item.
- **Homeowner suppression** is the NEXT project after this one.

## 2. Where everything is

- **Backend worktree:** `C:\Users\Windows\bl-wt\profile-be`.
  - Branch: **`feat/account-deletion-p3a`**, local only, NOT pushed.
  - Commits on top of `origin/main`:
    - `8d7baf71` docs: retention matrix
    - `ee314148` docs: owner sign-off on the matrix
    - `353c25fb` **wip(db): migration 113** (untested, unreviewed)
    - `0e3541f6` docs: build journal + this handoff
- **Frontend worktree:** `C:\Users\Windows\bl-wt\profile-fe`. Not needed until P5.
- **Never** use the shared Desktop checkouts for code; they are stale. The Desktop repo is used only for `railway` commands, because it is the railway-linked one.
- **Remote main = Railway deploy (backend). Remote master = Vercel deploy (frontend).**

### Active files

| File | Role |
|---|---|
| `alembic/versions/113_account_deletion_purge.py` | **THE WIP.** Fence triggers + 4 SECURITY DEFINER functions + grants/policies + ownership hand-over + downgrade |
| `alembic/versions/112_account_deletion_lifecycle.py` | live; the pattern to copy (role guard, Supabase revokes, temporary SET/CREATE hand-over, membership check) |
| `docs/product/account-deletion-retention-matrix.md` | **the spec** the purge must meet (§2 per table, §3 outside Postgres, §4 acceptance tests, §5 owner decisions) |
| `docs/product/account-deletion-m113-design.md` | the 113 design that went to the Codex consult |
| `docs/product/account-deletion-and-export.md` | overall design + §4 owner decisions |
| `tasks/todo-account-deletion.md` | phased plan + **every Codex round's findings and how each was handled** |
| `src/api/routes/auth_helpers/account_deletion.py` | live P2a request/restore logic (lock_user, step-up, in-txn revoke) |
| `src/api/auth.py` | live P2b gate `_refuse_if_pending_deletion` (allowlist GET /auth/me, POST /auth/account/restore) |
| `src/api/quota.py` | `quota_block_reason` refuses pending accounts (worker gate) |
| `scripts/provision_rls_roles.sql` | role/grant mirror ("Role 4: bridgeleads_purge" block): **113's grants must be mirrored here** |
| `tests/test_account_deletion_lifecycle.py`, `_routes.py`, `_gate.py` | live tests (P1, P2a, P2b) |

## 3. What migration 113 contains (and why)

- **`account_deletion_fence()`:** a BEFORE INSERT OR UPDATE row trigger, `ENABLE ALWAYS`, SECURITY INVOKER.
  - On 18 tables with `user_id`; `job_logs` gets its own variant that resolves the owner through `jobs`.
  - Locks the owning users row `FOR KEY SHARE`. Raises BLD20 if the state is not NULL or pending, and BLD21 if `user_id` changes.
  - Writes made as `current_user = bridgeleads_purge` pass.
  - Billing ledgers are deliberately NOT fenced.
- **`claim_account_deletion(lease)`:**
  - Picks the candidate through the **users** row (`FOR NO KEY UPDATE OF u SKIP LOCKED`), giving users-first lock order everywhere.
  - Moves pending → purging only when it is due AND `stripe_state` is `cancel_set` or `not_applicable`.
  - Or reclaims an expired lease, rotating the token.
  - Respects `next_attempt_at`.
- **`record_deletion_progress(id, token, phase, from, to, error)`:**
  - **Stripe:** compare-and-set per status.
  - **`scheduled_email_sent`:** pending rows, no token.
  - **`r2_first_sweep` / `final_email_sent` / `tombstoned` / `r2_final_sweep`:** need the live token.
  - **`tombstoned`** releases the lease and parks the row until first sweep + 24 h.
  - **`error`:** backoff.
- **`purge_account_data(id, token, cache_keys[], batch)`:**
  - Takes `users FOR UPDATE` first; this waits for writers already past the fence.
  - Re-verifies the claim. Requires `r2_first_sweep_at`. Returns early if `db_purged_at` is set.
  - Runs the matrix: deletes, scrubs, deletes `skip_trace_cache` rows by keys and by `results.skip_trace_subject_hash`, deletes `pending_registrations` by email_hmac, upserts the trial HMAC keeping the longer expiry.
  - Batches results and property_list_membership. Returns false while more remains; sets `db_purged_at` with the last batch.
- **`complete_account_deletion(id, token)`:**
  - Requires every marker and a 24 h gap between the R2 sweeps.
  - Re-scrubs `audit_events.detail`.
  - Moves purging → deleted.
- **Why skeletons, not deletes:** `jobs`, `results`, `scraper_configs` and `scraper_batches` CASCADE into the billing ledgers, so they are scrubbed, not deleted.
- **Why batched:** the largest prod account has 106k results. Prod `statement_timeout` = 2 min.
- **Verified so far:** only `alembic upgrade head` → `downgrade 112` → `upgrade head` on the local superuser DB, plus ruff.

## 4. Next steps, in order

1. **Re-read the 113 file in full and fix anything wrong.**
2. **Write `tests/test_account_deletion_purge.py`**, real DB, no mocks:
   - **Fence:**
     - a writer paused between the trigger and commit (two sync connections + a thread): the purge waits, then cleans its row;
     - an insert after the claim → BLD20;
     - a multi-row insert;
     - re-parenting `user_id` → BLD21;
     - `job_logs` resolved via `jobs`;
     - writes by the purge role pass;
     - app/system role writes (`@pytest.mark.integration`, skip if roles are absent).
   - **Functions:**
     - claim only when due and Stripe is OK;
     - reclaim rotates the token, and the old token gets BLD33;
     - progress phase rules and the compare-and-set;
     - the purge end-state per matrix §4: DELETE tables empty for the user, SCRUB columns NULL, KEEP ledger counts unchanged, another tenant byte-identical, `pending_registrations` gone, trial row present;
     - batching returns false, then true;
     - complete refuses until every marker plus the 24 h gap.
   - Put an account into `purging` the way the P1/P2 tests do: `SET LOCAL ROLE bridgeleads_purge` on a sync connection, or call the claim function after making the row due.
   - **Measure the fence cost:** insert ~10k results with and without the trigger, and report it.
3. **Login + refresh → 401 (not 500)** for accounts that are purging or deleted, since the fence turns a `user_sessions` insert into BLD20. See `src/api/routes/auth_helpers/login.py`.
4. **Mirror 113's grants** in `scripts/provision_rls_roles.sql`, and mirror the policies if needed.
5. **Production-like simulation (mandatory: production migrates as a non-superuser).** Recreate it:
   - `DROP/CREATE DATABASE bl_purgesim_test OWNER bl_sim_owner`. `bl_sim_owner` is `LOGIN NOSUPERUSER CREATEROLE BYPASSRLS`. Create `anon` and `authenticated` NOLOGIN and `service_role` NOLOGIN BYPASSRLS if absent. Connect via `template1`; there is no `postgres` database locally.
   - `alembic upgrade 112` as the superuser URL. Then ALTER OWNER of every table, sequence, view and function in public to `bl_sim_owner`.
   - `GRANT bridgeleads_purge TO bl_sim_owner WITH ADMIN TRUE, INHERIT FALSE, SET FALSE`.
   - `GRANT ALL ON users TO anon, authenticated, service_role`.
   - `ALTER DEFAULT PRIVILEGES FOR ROLE bl_sim_owner IN SCHEMA public GRANT ALL ON TABLES` / `EXECUTE ON FUNCTIONS` `TO anon, authenticated, service_role`.
   - Then, as `bl_sim_owner` (`TEST_DATABASE_URL_SYNC` = `DATABASE_URL_SYNC` = the sim URL): `upgrade head`, `downgrade 112`, `upgrade head`.
   - Check:
     - zero leaked privileges for anon / authenticated / service_role / bridgeleads_system / public on the new functions (prove the check catches a hand-granted leak);
     - the functions are owned by bridgeleads_purge;
     - the only purge membership is the inert ADMIN row.
   - Then drop the sim DB and the sim roles.
6. **Codex diff review → GATE PASS.**
   - Prompt via stdin from the scratchpad: `codex exec -c mcp_servers={} --skip-git-repo-check - < prompt.txt`, with a header "DO NOT run shell, read files, or use git" followed by the diff.
7. **PR → CI** (~30 min) → **ask the owner** → merge → announce to peer sessions (ListAgents; frontend peer `web-scrapper-automation-1c`; backend peer -76 is gone) → verify prod read-only → post VERIFIED.
8. **P3b beat task:**
   - Drive Stripe cancel/uncancel from `stripe_state`, using idempotency keys `acctdel-<id>-<action>`.
   - Send the scheduled email for pending rows.
   - In-flight precondition: no non-terminal jobs, batch runs or skip-trace queues, until day 40. Alert ops on day 40 if Stripe is still unconfirmed.
   - Then:
     1. claim;
     2. R2 sweep 1 (prefix `exports/{user_id}/` plus the keys referenced by `jobs.export_key` / `batch_runs.combined_export_key`, captured first);
     3. purge batches (compute cache keys with `pending_row_subject_key` BEFORE the scrub);
     4. final email;
     5. tombstone in Python (Fernet placeholder `deleted+<id>@invalid`);
     6. reclaim after 24 h;
     7. R2 sweep 2 (fail closed);
     8. complete;
     9. delete the Stripe Customer once the subscription has ended.
9. **P4 export, P5 frontend**, then the owner flips the flag.

## 5. Environment

- **Test venv:** `C:\Users\Windows\bl-profile-venv` (`$PY`).
- **Isolated test DB:** `bridgeleads_profile_test` on local PG16 :5432 (superuser `bridgeleads`), Redis db 11.
- **Env script:** `C:\Users\Windows\AppData\Local\Temp\claude\C--Users-Windows-OneDrive---Seattle-Colleges-Desktop-web-scrapper-automation\b7292346-e297-4e57-80ff-75d3b68773c2\scratchpad\env.sh`.
  - `source` it. It sets `TEST_DATABASE_URL(_SYNC)`, `DATABASE_URL(_SYNC)`, `REDIS_URL`, `SECRET_KEY` and `$PY`, and cds into the BE worktree.
  - Before alembic: `unset DATABASE_URL_MIGRATE; export ENVIRONMENT=test`.
  - If the file is gone, recreate it per memory `reference_isolated_pytest_db_no_interference`.
- **Run tests with a log file and check `$?`:**

  ```
  $PY -m pytest <files> -q -p no:cacheprovider -m "integration or not integration" > log 2>&1; echo $?
  ```

  Never pipe to tail.
- **OpenAPI regen:** `$PY scripts/export_openapi.py`. The diff should have 0 deletions.
- **Prod read-only checks:** `railway run --service worker <venv python> -u script.py` from the Desktop repo.
  - Use `DATABASE_URL_MIGRATE` with `set_session(readonly=True)`.
  - Never print the DSN.
- **Prod facts:**
  - PG 17, migrated as `postgres`: NOT superuser, CREATEROLE + BYPASSRLS.
  - Supabase default privileges grant ALL on new tables / EXECUTE on new functions to anon, authenticated and service_role. Revoke them on every new object.
  - At handoff, production had 0 rows in `account_deletions`.

## 6. Failed attempts / landmines from this session

- **Background waiters die on low memory.** CI pollers were killed twice. Poll in the foreground in ≤10-minute loops.
- **A stray `cat > file` with no stdin hung a tool call.** Write helper scripts with the Write tool.
- **Nested quotes broke a bash heredoc in a Python patch script.** Write the script to a file and run it.
- **`sed` edits inside quoted check scripts broke their quoting.** Rewrite the file instead.
- **Tests that passed for the wrong reason:**
  - A download link minted in the same second as the deletion was refused by the older whole-second cutoff, not by the new belt. The test now sleeps more than 1 s and was mutation-checked.
  - Audit events were counted before the fire-and-forget write landed. Tests now `await asyncio.gather(*security._audit_tasks)`.
- **`SET ROLE` / `SET SESSION AUTHORIZATION` are checked against the SESSION user** (the superuser in tests), so they prove nothing. Assert role membership with `pg_has_role` instead.
- **`interval '30 days'`** follows the session time zone, and DST shifts it an hour locally. Tests use ±2 h tolerance.
- **SQLAlchemy has no `with_for_update(no_key=True)`.** `key_share=True` renders `FOR NO KEY UPDATE`.
- **The P2b gate pre-empts later checks.** A pending account's repeat delete and API-key mint now get 403 from the gate.
- **The backend peer session -76 disappeared.** Only `-1c` (frontend) remains. Announce merges to it.

## 7. Open items for counsel / owner (not blocking P3)

- Tracerfy contract deletion path.
- California coverage and data-broker status.
- WA sales-tax location fields.
- FCRA prohibited-use clause.
- Downloaded-files policy wording.
- Supabase backup window.
- The published privacy contact (`bridgeleads.com`) does not receive mail. Fix this before the homeowner suppression project.
