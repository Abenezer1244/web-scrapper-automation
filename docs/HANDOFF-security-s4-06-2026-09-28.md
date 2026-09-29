# Handoff: audit #4 PRs + S4-06 shipped (2026-09-28), what is left

**Read this first. Then read the top entry of `docs/BUILD_JOURNAL.md` (2026-09-28, "Audit #4 PRs
merged, S4-06 fixed, and a merge gate that passed on a crash") and the "Audit #5" section at
the END of `SECURITY-AUDIT.md`.** The previous handoff is
`docs/HANDOFF-security-audit5-2026-09-28.md` on branch `docs/handoff-security-audit5`
(worktree `C:/Users/Windows/bl-wt-secaudit5-handoff`, pushed, not merged). Its §4 "failed
attempts" and §6 "test env" still apply.

## 1. Goal

Work the BridgeLeads security remediation queue left open by audits #4 and #5. This
session's owner instructions:
1. Merge the open audit #4 PRs #374 then #378 (merge = deploy, so run the quiet check first).
2. Fix S4-06 on a new branch: extract the reservation, point the tests at it, write a
   lock-wait boundary test and prove it fails, move the clock read after the lock. Consult
   Codex before and have it review after.

Both are done. The owner then said "merge", so #385 was merged, and the journal went in as #387.

## 2. Current state (verified 2026-09-29 ~01:56Z)

- **main head: `fa658ccf`** (#387). Production (Railway api/worker/beat) is SUCCESS on it,
  `/health` 200 in 0.3 s, worker boot clean.
- Merged and live this session:

| PR | Finding | Merge commit | What |
|---|---|---|---|
| #374 | S3-03 / S4-01 (P1) | `bdbbc762` | paid skip trace only for accounts that may buy it: trial lifetime allowance (`SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE`, default 25), Starter/frozen/ended blocked; `account_charge_block` in `run_scrape_job` |
| #378 | S4-03 (P2) | `4f56a5e3` | batch and segment CSV exports share the `export` rate-limit zone |
| #385 | S4-06 (P2) | `06f23df6` | reservation clock read after the users-row lock (details below) |
| #387 | docs | `fa658ccf` | BUILD_JOURNAL entry |

- **Nothing of mine is open or unpushed** except this handoff (branch `docs/handoff-s4-06`).
- **Another session is active on main** (it merged #384 "lookup 1b-1c-i planner" at
  `9833a7b0` mid-session, and a #386 exists). Expect main to move; re-fetch before any merge.

### What #385 (S4-06) changed

- `src/workers/tasks.py`:
  - `account_charge_state(db, user_id, *, lock) -> (block, now)`: reads the users row
    under `lock`, THEN reads `clock_timestamp()`; `block` is 'frozen'/'ended'/None.
  - `account_charge_block(...)` now wraps it (unchanged signature and behaviour for its callers).
  - `reserve_job_quota(db, *, job_id, user_id, want) -> int | None`: the reservation
    formerly inline in `run_scrape_job`. Steps:
    1. CAS on jobs (`reserved_at = clock_timestamp()` as a claim marker; returns None if lost).
    2. `account_charge_state(FOR UPDATE)` (a block means want=0).
    3. The grant with lazy rollover at the post-lock `now`.
    4. Record `reserved_count`, `quota_period_start` AND `reserved_at = now` on the job.

    Lock order is unchanged: jobs, then users. Does not commit.
  - `run_scrape_job` calls it; None means a watchdog re-run, which reuses
    `jobs.reserved_count`.
- `tests/test_quota_reservation.py`: `_reserve()` calls the real function; the
  `_RESERVE_SQL` copy is gone. `_move_window_back()` replaces the old test-only `at=`. New
  test `test_a_reservation_that_waits_on_the_user_lock_across_the_window_end_charges_the_NEW_window`:
  - Boundary at DB clock + 10 s; a holder session holds the users row.
  - Asserts the reservation is WAITING in `pg_locks` before the boundary, then commits
    after it.
  - Result: FAILED on the pure extraction ("granted 100"), passes on the fix (300).
- Plan and review: `tasks/todo-security-s4-06.md`.
- Invariant relied on: `users.quota_anchor_at/quota_period_start/quota_period_end` are NOT
  NULL and `public.quota_next_start` is STRICT, so `new_start` is never NULL.
  `reservation_is_current_sql` reads `jobs.reserved_at` only when the job's window is NULL
  (pre-088).
- Codex: design consult GATE PASS; diff review r1 GATE PASS (one P3, test margin 5 s to
  10 s, adopted).

## 3. What is left (priority order)

1. **Owner:** check the Supabase usage/health dashboard for the **DB stall ~01:28-01:45Z
   2026-09-29**:
   - every worker task took 60-130 s, and beat tasks failed with
     `SSL connection has been closed unexpectedly`;
   - a `pg_type` query took 45 s, and the pooler returned `ECHECKOUTTIMEOUT`;
   - it started BEFORE #385 merged (quiet was 0 at ~00:58, pool timeouts at ~01:30);
   - it recovered by itself; root cause unknown and looks DB-wide.
2. **S4-02 (P2), next code item:** `src/db/models.py:817-844`, `Job.RUN_SLOT_CANCEL_COOLDOWN_SECONDS = 300`
   and `Job.holds_run_slot(now)`.
   - A cancelled run whose worker had started holds the scraper's run slot for a FIXED
     300 s after `finished_at`.
   - The worker can still be scraping after that (it only notices the cancel at a check),
     and its dedup claims can then freeze a new run's leads as "already delivered".
   - The heartbeat skips cancelled rows, so liveness is not observable today.
   - Every start path uses `holds_run_slot` (POST /jobs, scheduler, batch fan-out).
   - Fix direction is **not decided**; brainstorm it and consult Codex first. Candidate
     ideas to evaluate, not settled:
     - the worker acknowledges the cancel (e.g. stamps a "stopped" marker when it exits),
       and the slot is held until that marker OR a much longer hard ceiling;
     - or keep heartbeating cancelled-but-running jobs.
   - Watch the landmines `concurrent_deploys_kill_long_jobs` and
     `celery_timeout_read_as_permanent`.
3. **5b-ii (P3):** move these onto `pinned_session()` and map `is_blocked_destination` the
   way `src/utils/safe_http.py` `_get` does:
   - `src/scrapers/enrichment/pacs.py:137`
   - `src/scrapers/templates/acclaimweb.py:1042,1067`
   - `src/scrapers/enrichment/skip_trace.py:630,702`

   They currently validate, then fetch on a raw `requests.Session()`.
4. **D5-03 rollout (owner/ops):** `SCRAPER_EGRESS_PROXY_ENABLED` is OFF. Run every county
   template through the SOCKS5 proxy (staging or a quiet window, gently) before turning it
   on. So far it is proven only on `atip.piercecountywa.gov`.
5. **`.env.example`:** add `SCRAPER_EGRESS_PROXY_ENABLED=false` and
   `SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE=25`, and confirm #373's two lines are placeholders. A
   permission rule blocks Claude from reading this file, so **the owner must do it or grant
   access.**
6. **D5-01 (P3, product decision):** apply `AI_JOB_LIMITS` in scheduler/batch dispatch? Ask the owner.
7. **S3-16 (P2, external):** Tracerfy must send `X-Tracerfy-Webhook-Secret`; then rotate the
   secret and delete the legacy path-secret route (`src/api/routes/webhooks.py:169`).
8. **Owner infra:**
   - Cloudflare as sole ingress (S3-04)
   - admin password rotation (S3-17)
   - GH `production` environment protection
   - Tracerfy spend caps
9. **Known limitation (documented, not scheduled):** an account with no record limit that
   is still live skips the LOCKED frozen/ended re-check in `run_scrape_job` (only the
   unlocked pre-read runs).
10. **Forward note (#382):** a future API reader of the skip-trace pause state must read
    only the caller's own `<user_id>` field plus `global`/`account_default`.

## 4. Failed attempts and dead ends (do not repeat)

- **I merged #385 on a crashed quiet check.** The one-shot script said "merge unless a
  count is non-zero"; `quiet.py` crashed (pool timeout), printed no counts, and the merge
  ran.
  - **Rule:** run `quiet.py` and the merge as SEPARATE tool calls. Require exit 0 AND all
    four count lines present AND each 0 (see the check in §7).
  - Memory: `landmine_quiet_gate_must_require_zeros`.
- **CRLF:** working copies are CRLF (`core.autocrlf=true`), blobs are LF.
  - A Python edit that adds `\r\n` to text already containing `\r` produced `\r\r\n`. The
    mixed file then committed as a whole-file rewrite (18k-line diff for a 64-line entry).
  - Fixed by rebuilding from the LF blob and amending the unpushed commit.
  - Check `git diff --stat` after any scripted edit.
- **Background CI watchers get reaped** ("stopped because the system is running low on
  memory"). Poll in the FOREGROUND in blocks under 10 min (loop over `gh pr checks <n>`,
  30 s sleep, up to 18 iterations). CI `Test` takes ~16-21 min.
- **Strict branch protection:** a PR BEHIND main cannot merge; merge main into the PR
  branch (a push), re-run the affected tests locally, and wait for CI again. Another
  session merging in between forces another round (happened on #374).
- `codex exec -s read-only` with the code/diff INLINE, run from the scratchpad dir, worked
  for both the consult and the review (5-7 min at `model_reasoning_effort="high"`).
  - Open the prompt with "Do NOT load any skill, do NOT run /graphify or any preamble, do
    NOT run shell, read files, or git."
  - Never `codex review` (it runs pytest on your test DB).

## 5. Where things are on disk

| Path | Branch | State |
|---|---|---|
| `C:/Users/Windows/bl-wt-s406` | `docs/handoff-s4-06` | this handoff (committed locally). Earlier branches here: `fix/security-s4-06-reservation-clock` (= #385, merged), `docs/journal-2026-09-28-s4-06` (= #387, merged) |
| `C:/Users/Windows/bl-wt-merge374` | `chore/security-audit4-delta` | #374, merged; no longer needed |
| `C:/Users/Windows/bl-wt-secaudit4` | `fix/security-audit4b-export-zone` | #378, merged; no longer needed |
| `C:/Users/Windows/bl-wt-secaudit5*` | various | audit #5 (see its handoff) |
| scratchpad `.../a3e83a80-.../scratchpad/s406/` | n/a | Codex prompts/outputs: `consult.txt/.out`, `review1.txt/.out` |

The owner has not decided whether to remove the stale worktrees. **Never delete or
force-move branches** (memory `feedback_no_branch_delete_shared_onedrive`). Removing a
worktree folder needs the owner's OK.

## 6. Test environment

- Python: `C:/Users/Windows/bl-rescat-venv/Scripts/python.exe`.
- `source C:/Users/Windows/bl-wt-secaudit5/.unlazy/secaudit5/testenv.sh` (DB
  `bridgeleads_secaudit5_test`, Redis index 5; others: `testenv-b|d|e|f.sh`, Redis 6-9).
  Then run:
  `cd <worktree> && $PY -m alembic upgrade head` (fresh DB only), then
  `$PY -m pytest <files> -q -p no:cacheprovider -o addopts=""`.
- **Never bare pytest** (memory `bare_pytest_uses_prod_env`). Kill only your own orphaned
  pytest processes (check the parent command line).
- Suites used for the reservation/billing area:
  - `test_quota_reservation`, `test_audit4_paid_skip_trace_gate`;
  - `test_quota_accounting`, `test_code_violation_plan_cap_order`, `test_tax_plan_cap_order`,
    `test_skip_trace_over_quota`, `test_entitlement_lifecycle`, `test_plan_change_billing`,
    `test_undelivered_run_rows`;
  - `test_workers`, `test_batch_recovery`, `test_claim_transfer`,
    `test_results_already_delivered`, `test_skip_trace_already_delivered`,
    `test_skip_trace_enqueue_after_delivery`.

## 7. Merging (merge = deploy)

1. `git fetch origin`; PR must be CLEAN (`gh pr view <n> --json headRefOid,mergeStateStatus`),
   CI `Test` + `Dependency Audit` green (docs-only PRs skip them).
2. Quiet check, as its own call:
   `railway run --service worker C:/Users/Windows/bl-rescat-venv/Scripts/python.exe C:/Users/Windows/bl-checks/quiet.py > $TEMP/quiet.txt 2>&1; echo exit=$?`
   Then require exit 0 and 4 lines matching `^\s+0\s+(jobs not terminal|pending skip-trace rows ACTIVE|other sessions in a transaction|locks held)`.
3. `gh pr merge <n> --merge --match-head-commit <full sha>`. Never `--admin`.
4. Poll `railway deployment list --service {api,worker,beat} --json` for SUCCESS on the
   merge commit (match `meta.commitHash`). Then check `/health` (domain from
   `railway variables --service api --json` `RAILWAY_PUBLIC_DOMAIN`) and grep the worker
   logs for `ready.` / `raised unexpected`.

## 8. Next step

1. `git fetch origin`. Read this file, the journal's top entry, and `SECURITY-AUDIT.md`
   (Audit #5 section at the end).
2. Ask the owner two things:
   - whether to push/merge this handoff branch (`docs/handoff-s4-06`, docs only);
   - whether to remove the stale worktrees `bl-wt-merge374` and `bl-wt-secaudit4`
     (folders only, never branches).
3. Start **S4-02** on a NEW branch off `origin/main` in its own worktree (suggested:
   `fix/security-s4-02-run-slot-cooldown`, `C:/Users/Windows/bl-wt-s402`):
   - write the plan in `tasks/todo-security-s4-02.md`;
   - brainstorm the approach, then consult Codex (inline code, `codex exec -s read-only`);
   - write a regression test that reproduces a cancelled-but-still-scraping worker
     outliving the 300 s slot, and PROVE it fails on main;
   - fix; run the related suites on its own `_test` DB;
   - Codex diff review until GATE: PASS.
   - No push or merge without the owner's OK.
