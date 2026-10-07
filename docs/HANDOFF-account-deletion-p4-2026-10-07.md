# HANDOFF: account deletion — P3 done and live; next is P4 (data export) — 2026-10-07

Read this whole file first, then the files in §4. Then follow CLAUDE.md:
- **Codex in the loop on every step:**
  - design consult before code;
  - diff review until GATE PASS;
  - per the cross-check doctrine in `.claude/rules/codex-collaboration.md`.
- **Security baseline:** `.claude/rules/security.md`.
- **No mocks.** External APIs (Stripe, Resend, R2) are passed in as stand-ins. Precedents: `_expire_trials_impl(subscription_lookup=)` and `account_deletion_beat._drive_account_deletions_impl(stripe_api=, send=, r2=, send_final=)`.
- **Merge = deploy** (Railway, backend = `main`; Vercel, frontend = `master`).
- **Ask the owner before merging ANY PR, and before ANY migration.**
- **Never switch `ACCOUNT_DELETION_ENABLED` on.** The owner does that after P5.

## 1. The goal

Profile follow-up 5, "Delete my account + Export my data", built in phases:

| Phase | What | State |
|---|---|---|
| P1 | migration 112: lifecycle schema, NOLOGIN `bridgeleads_purge` role, request/restore functions | LIVE (BE #472) |
| P2a | `POST /auth/account/delete` and `/auth/account/restore`, behind `ACCOUNT_DELETION_ENABLED` | LIVE (BE #473), flag OFF |
| P2b | 403 gate for pending accounts (only `GET /auth/me` and restore are allowed), the worker refusal, the download-link belt | LIVE (BE #474) |
| P3a | migration 113: write fence + claim/progress/purge/complete definer functions | LIVE (BE #476, `5b7a00aa`) |
| P3b-1 | beat `drive_account_deletions` (Stripe cancel/uncancel, "scheduled for deletion" email, overdue alert, skip-trace gate) + migration 114 (the worker may READ `account_deletions`) | LIVE (BE #477, `724d97f8`) |
| P3b-2 | the purge driver (defer while work is in flight, claim, R2 sweeps, batched purge, final email, tombstone, 24 h second pass, complete, Stripe Customer delete) | LIVE (BE #479, `181e2e1c`) |
| **P4** | **`POST /auth/export`: a ZIP of the user's data with a 7-day link** | **NOT STARTED: next** |
| P5 | frontend: Settings > Account > Your data (export button, delete dialog, grace banner + Restore) | not started |
| — | owner flips `ACCOUNT_DELETION_ENABLED=true` | only after P4 + P5 are live and verified |

Owner decisions (final; see `docs/product/account-deletion-and-export.md` §4):
- 30-day undoable grace period.
- Billing: cancel at period end, no refund.
- Retention: billing records 7 years; audit events 24 months (high-value) and 12 months (routine); `delivered_records` 24 months.
- Stripe: never redact. Delete the Customer only after the subscription has ended.
- Lead rows keep address + parcel.
- Trial-abuse email HMAC kept for 2 years.
- Tracerfy copies are a counsel item.
- Homeowner suppression is the NEXT project after this one.

## 2. Where everything is

- **Backend worktree:** `C:\Users\Windows\bl-wt\profile-be`.
  - Branch: **`feat/account-deletion-p4`**, cut from `origin/main` @ `181e2e1c`. It holds only this handoff commit.
  - Never use the Desktop checkouts for code; they are stale. The Desktop repo is used only for `railway` commands, because it is the railway-linked one.
- **Frontend worktree:** `C:\Users\Windows\bl-wt\profile-fe` (for P5).
- **Helper scripts:** `C:\Users\Windows\bl-wt\tools\`, copied out of the session scratchpad.

| Script | What it does |
|---|---|
| `env.sh` | `source` it. Sets `TEST_DATABASE_URL(_SYNC)`, `DATABASE_URL(_SYNC)`, `REDIS_URL` (db 11), `SECRET_KEY`, `$PY` (`C:\Users\Windows\bl-profile-venv`), and cds into the BE worktree. Before alembic: `unset DATABASE_URL_MIGRATE; export ENVIRONMENT=test`. |
| `purgesim.py` | The **prod-like non-superuser migration simulation** (mandatory for every migration). Creates `bl_purgesim_test` + `bl_sim_owner` (NOSUPERUSER CREATEROLE BYPASSRLS), anon/authenticated/service_role, Supabase default privileges. Runs upgrade 112 → head → downgrade 112 → head as the non-superuser, checks leaked privileges (proven to catch a hand-granted leak), function owners, purge membership, triggers. Then drops everything. Run: `source env.sh; $PY purgesim.py`. Extend its `fns` dict for new functions. |
| `prod_verify_113.py`, `prod_verify_114.py` | READ-ONLY production checks. Run from the Desktop repo: `railway run --service worker <venv python> -u <script>`. They use `DATABASE_URL_MIGRATE` with `set_session(readonly=True)` and never print the DSN. |
| `prov_check.py` | Executes the `DO $purge$` block of `scripts/provision_rls_roles.sql` against the test DB, rolled back. |
| `r2_list_check.py` | READ-ONLY: proves `DataExporter.list_r2_keys` against production R2. |
| `q.py` | One-off SQL against the test DB (`$PY q.py "select ..."`). Does not commit. |

- **Test DB:** `bridgeleads_profile_test` on local PG16 :5432 (superuser `bridgeleads`), at alembic 114. Redis db 11.
- **Run tests with a log file and check `$?`; never pipe to tail:**

  ```
  $PY -m pytest <files> -q -p no:cacheprovider -m "integration or not integration" > log 2>&1; echo $?
  ```

  - The full local suite takes about 60 min. CI takes about 30 min.
  - pytest-timeout is not installed (`--timeout` is an error).

## 3. What P3 built (what P4 must fit)

- **`src/workers/account_deletion_beat.py`**: the beat, every 5 min, one run at a time under advisory lock `7_113_000_001`. In order:
  1. Stripe reconcile;
  2. scheduled notice;
  3. drain the in-flight deferral (before ANY claim);
  4. purges, each claim stopping before its lease or the tick budget runs out;
  5. Stripe Customer close;
  6. overdue/stuck alerts.
- **The R2 sweep deletes everything under `exports/{user_id}/`**, so a P4 export written under `exports/{user_id}/account/` is automatically swept at purge. Keep it under that prefix.
- **`DataExporter.list_r2_keys(prefix)`** (`src/utils/data_exporter.py`) uses the native Cloudflare API. Production's S3 keys get **Unauthorized** on ListObjects; the native API token can list. Deletes use `delete_from_r2` (404-safe).
- **Fence (113):**
  - an INSERT for a purging/deleted owner raises BLD20;
  - an UPDATE keeps the SCRUB columns at their old values;
  - `user_id` is immutable (BLD21);
  - `skip_trace_queues` has its own non-raising trigger, and its `user_id`/`tracerfy_queue_id` are immutable.
- **The skip-trace access rule** (`src/workers/skip_trace_claim.py`): any `deletion_state` means `ended`.
- **The P2b download belt** (`src/api/routes/jobs.py:2317`): emailed download links are refused for ANY non-NULL `deletion_state`. **P4 design question:** the design doc §2 says a user may download an export made BEFORE the deletion request during the grace period, but the current belt refuses it. Decide with Codex and the owner.

## 4. Files to read for P4

| File | Why |
|---|---|
| `docs/product/account-deletion-and-export.md` §3 | The export spec: `POST /auth/export` (session + password), worker job, one ZIP (profile JSON, scraper configs, schedules, batch history, every lead CSV the user can already download), `DataExporter` (CSV-injection sanitised), expiring signed link by email and in-app, one export per 24 h, same RLS + `user_id` filters |
| `tasks/todo-account-deletion.md` | Plan + every Codex round (the P4 line: worker ZIP under `exports/{user_id}/account/`, 7-day signed link) |
| `docs/product/account-deletion-retention-matrix.md` | What data exists per table |
| `src/api/routes/auth_helpers/account_deletion.py`, `src/api/routes/auth.py` | Step-up pattern (`_reauthenticate`, MFA), audit events, the `ACCOUNT_DELETION_ENABLED` gate |
| `src/api/auth.py` `_refuse_if_pending_deletion` | The 403 allowlist. Export creation while pending must be 403 (the dialog says "download your data first") |
| `src/utils/data_exporter.py` | `export()` is the ONLY entry point (`.claude/rules/exports.md`); `upload_to_r2`, `get_download_url`, `sanitize_for_csv` |
| `src/api/routes/jobs.py` around 2300 | The signed download-token pattern + the deletion belt |
| `src/workers/account_emails.py` `_send` | Email helper pattern |
| `scripts/export_openapi.py` | Regenerate OpenAPI after schema/route changes (diff must have 0 deletions); a BE schema merge needs a FE types-regen PR |

## 5. Changes made in the last session (2026-10-06/07)

**Migration 113 (#476).** Found by a code audit: beat sweeps UPDATE many tenants' rows in one statement, so a raise-on-UPDATE fence would cause a global outage. The owner chose the pin design. In 113:
- a definer owner-state lookup, so the caller's RLS can never make the fence fail open;
- the trigger is named `zz_` so it runs last;
- the claim takes users `FOR UPDATE SKIP LOCKED` (it skips accounts with a write in flight), sets `is_active=false` (every sign-in path returns 401) and withdraws queued lookups;
- the purge is re-runnable;
- complete refuses (BLD36) while scrubbed data reappeared or a linked batch is still pending;
- a tombstone invariant is checked in SQL;
- the audit `detail` fence;
- DELETE/TRUNCATE on the billing skeletons is revoked from the Supabase API roles;
- `tracerfy_queue_id` is pinned.

Codex took 11 rounds to PASS.

**Migration 114 + the P3b-1 beat (#477)**, Codex 2 rounds. **The P3b-2 driver (#479)**, Codex 7 rounds. All three were prod-verified read-only after merging (`gh pr comment` VERIFIED on each PR).

**Tests:**
- `tests/test_account_deletion_purge.py`: 113 at SQL level, including concurrency tests (mutation-checked).
- `tests/test_account_deletion_beat.py`: 24 beat tests.
- Updated: `tests/test_account_deletion_lifecycle.py` and `tests/test_account_deletion_routes.py`.
- `tests/test_pierce_cv_owner.py`: re-parenting a config is now refused by the DB.

**Docs:** the build journal entry (2026-10-06 session 2), `tasks/todo-account-deletion.md` (all review rounds), the retention matrix (queue + audit rows).

## 6. Failed attempts / landmines (do not repeat)

- **Grants and ALTER OWNER:** a grant to `current_user` made BEFORE `ALTER ... OWNER` is dropped with the old owner's ACL. Grant as the new owner (`SET LOCAL ROLE bridgeleads_purge` inside the hand-over). Caught only by `purgesim.py`, so always run it for migrations.
- **`SET ROLE` in tests proves nothing:** it is checked against the superuser session. Assert with `pg_has_role` / `has_*_privilege` instead.
- **Python patches in bash heredocs broke twice** (unterminated strings, truncated input). Write patch scripts with the Write tool and run them.
- **Patch-script `assert s.count(a) == 1`** fails when the pattern is also a substring of another line. That's good: nothing gets written. Replace all occurrences deliberately.
- **`sleep N; cmd` chains are blocked** by the harness. Poll in loops of 10 minutes or less, or use `run_in_background`.
- **The local full suite shares the test DB.** Never run another pytest beside it: the conftest teardown DELETEs. pytest collects at start, so a fix made mid-run isn't tested.
- **Test cleanup:** `tests/test_account_deletion_purge._seed` writes a `pending_registrations` row (no FK to users). Clean it up (the beat tests' `_cleanup_cache` does), or `test_register_email_verification` counts extras. This failed CI once.
- **Pre-existing local failures:** 3 `test_contact_lookup_schema` worker-role failures and 1 `test_contact_lookup_quote` (no redis-server binary). They also fail at 112; CI skips them.
- **Windows clock resolution:** `time.monotonic()` ticks at about 15 ms, so deadline checks use `>=`.
- **DST:** `interval '30 days'` follows the session time zone, so tests use ±2 h tolerance.
- **Prod read-only checks:** `railway run --service worker <venv python> -u script.py` from the Desktop repo. Use `DATABASE_URL_MIGRATE` with `set_session(readonly=True)`, never print the DSN, and import `src.api` before `src.utils.data_exporter` (circular import).
- **Codex:** `codex exec -c 'mcp_servers={}' --skip-git-repo-check -s read-only - < prompt.txt`, run from a scratch dir. Header "DO NOT run shell, read files, or use git". Pass prior-round dispositions with evidence, and use `git diff origin/main...HEAD` (three dots).

## 7. Next steps, in order

1. **P4 design:**
   - Re-read §4. Write the P4 plan into `tasks/todo-account-deletion.md`.
   - Codex design consult; fold the findings in.
   - Settle the download-belt question (§3).
   - Check in with the owner on the plan before coding.
2. **Likely shape** (≤5 files per PR):
   - `POST /auth/export`: `require_session` + `_reauthenticate` + MFA; 403 while pending (gate); one per 24 h (a durable record: probably a small `account_exports` table = a migration, so ask first, or reuse an existing table).
   - A Celery task that builds the ZIP via `DataExporter`, with every query filtered by `user_id`.
   - Upload under `exports/{user_id}/account/<id>.zip`.
   - Email the 7-day signed link (`get_download_url`, 604800 s) and show it in-app.
   - Audit `account_export_requested`.
   - Regenerate OpenAPI → FE types-regen PR.
3. Tests (real DB + stand-ins) → Codex diff review to GATE PASS → PR → CI → **owner OK** → merge.
   - Announce to the FE peer session `web-scrapper-automation-1c` (ListAgents / SendMessage): "MERGING NOW … hold BE merges"; later "VERIFIED".
   - Verify prod read-only, then post VERIFIED on the PR.
4. P5 frontend in `bl-wt/profile-fe` (branch from `origin/master`).
5. The owner flips the flag. Append a build-journal entry at the end of every substantial session.

## 8. Open items for counsel / owner (not blocking)

Tracerfy contract deletion path; California coverage / data-broker status; WA sales-tax location fields; FCRA prohibited-use clause; downloaded-files policy wording; Supabase backup window; the published privacy contact (`bridgeleads.com`) does not receive mail (fix before the homeowner-suppression project).
