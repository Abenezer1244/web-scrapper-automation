# Handoff: security audit #4 (delta) and fix phases, 2026-09-27

Resume here. Read this file first, then `SECURITY-AUDIT.md` (the "Audit #4" section at the top) and
`tasks/todo-security-audit4.md`.

## Where the work lives

| What | Value |
|---|---|
| Worktree | `C:/Users/Windows/bl-wt-secaudit4` (do the work HERE, not in the OneDrive repo root) |
| Branch | `chore/security-audit4-delta` (based on `origin/main` `f80f79ce`) |
| PR | **#374, OPEN, NOT merged, CI GREEN** (Test pass 18m, pip-audit pass). Merging deploys (Railway). |
| Commits on branch | `4505f87a` docs: audit #4 report; `f006197f` fix: Phase 4a (S3-03, S4-01) |
| Codex scratch worktree | `C:/Users/Windows/bl-wt-secaudit4-codex` (detached; `codex exec` edits whatever tree it runs in, so it only ever runs there) |
| Clean base worktree | `C:/Users/Windows/bl-wt-secaudit4-base` (detached at the old docs commit; for "does this fail on unmodified code" checks) |
| Local-only files (untracked, never commit) | `.unlazy/secaudit4/` (gates ledger, test env, suite scripts, patches) |

## The goal

The owner asked for a full 18-check security audit of BridgeLeads (tenant isolation, IDOR, admin routes, plan and
quota bypass, Stripe, Tracerfy, scraper jobs, live stream, CSV export, webhook SSRF, secrets, logs, errors, headers,
dependencies). Audit #3 had already run that exact checklist earlier the same day (report and 55 findings on main).
**The owner chose (AskUserQuestion): a DELTA audit of `786efcf0..ee601b55`, then fix in order, one PR per phase,
with owner approval before each PR merges.** Their trial decision: **trials get a small lifetime allowance of
contact lookups** (not zero, not unlimited).

Standing rules that apply: `CLAUDE.md` (phased work, max 5 files per phase, no mocks, Codex brainstorm before and
review after every build, Critical/High from either reviewer = NO-GO), `.claude/rules/security.md`,
`.claude/rules/testing.md`. No em dashes in user-facing copy.

## Current state

### Done
1. **Delta audit** (commit `4505f87a`): Claude reviewer (`tasks/audit4/delta-claude.md`) and Codex independently
   (`tasks/audit4/delta-codex.md`); every Codex-only finding verified in code by the driver. Report section
   "Audit #4" at the top of `SECURITY-AUDIT.md`. No IDOR, no injection, tenant isolation held in all changed code.
2. **Phase 4a** (commit `f006197f`, PR #374): fixes **S3-03** (trials bought paid Tracerfy lookups) and **S4-01**
   (nothing re-checked accounts that froze or ended after a job was queued).
   - `src/workers/skip_trace_claim.py`: `paid_lookup_access(user, now)` is THE rule; `read_access_rows(db, ids,
     lock=)` (allowed locks: `""`, `FOR NO KEY UPDATE`, `FOR UPDATE`, `FOR SHARE SKIP LOCKED`);
     `lifetime_credits_queued`; `claim_skip_trace_rows(..., report=None)` claims only what the account may buy,
     under `FOR NO KEY UPDATE` on the users row. `report` receives `{"access", "held"}` (not yet used by a caller).
   - `src/workers/skip_trace_dispatcher.py`: unlocked prefilter: Starter/ended accounts' queued rows are WITHDRAWN
     via `_cancel_undeliverable` (pending `cancelled`, lead back to `not_attempted`), frozen held queued;
     `_drop_blocked_accounts` re-checks the accounts about to be claimed `FOR SHARE SKIP LOCKED`.
   - `src/workers/tasks.py`: `account_charge_block(db, user_id, lock=)` (clock read AFTER the lock); job refused
     right after the claim if frozen/ended; at the quota reservation the decision is re-made under `FOR UPDATE`
     and grants 0; a blocked account enters the cap block even on an unlimited plan.
   - `src/config/settings.py`: `SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE: int = 25` (0 = no trial lookups).
   - Tests: `tests/test_audit4_paid_skip_trace_gate.py` (43, real DB, 2 thread-concurrency tests), each guard
     mutation-checked. `tests/test_skip_trace_dispatcher_claim.py` and `tests/test_skip_trace_spend_ledger.py`:
     fixture `starter_user` -> `business_user` (Starter rows are now withdrawn by design).
   - Rule table: Starter / frozen / ended = none; admin, `active`, `past_due` with grace, future
     `entitlement_ends_at` (paid through cancellation), no status AND no trial date (operator-granted) = full;
     everything else (app trial, `trialing`, `canceled`, unknown) = trial allowance.
   - Codex: plan consult (2 P1s folded in) + 4 review rounds; round 4 GATE: PASS.

### Open findings (in order of the agreed plan)
| Next | ID | Sev | What |
|---|---|---|---|
| **4b (next)** | S4-03 | P2 | `/batches/{id}/download`, `/batches/{id}/runs/{run}/download` (`src/api/routes/batches.py:733,854`), `/segments/intersection/export`, `/segments/union/export` (`src/api/routes/segments.py:709,835`) use zone `general`; move to `export` (20/min, per-process fallback when Redis is down). Only `jobs.py:1280,1439` use `export` today. |
| 4b | (UX) | - | Customer live-log line when leads are held by the trial allowance: `src/workers/tasks_helpers/enrich.py` ~2696 pass `report={}` to `claim_skip_trace_rows` and publish a message when `report["held"]`. No em dashes. |
| then | S3-08, S3-14 | P2 | audit #3 queue 2b: `safe_http` URL-parser SSRF (route through `pinned_session`), browser egress guard fails open |
| then | S3-15, S3-16 | P2 | audit #3 queue 2c: Tracerfy webhook body trusted for billing; legacy path-secret route on by default |
| later | S4-02 | P2 | run slot released 300 s after cancel even if the worker still runs (`src/db/models.py` `RUN_SLOT_CANCEL_COOLDOWN_SECONDS`); needs a worker-owned lease design |
| later | S4-06 | P2 | pre-existing: reservation `_reserved_at` clock read before the users lock (`src/workers/tasks.py` reservation) |
| later | S4-04 | P3 | pre-F-043 deleted scrapers keep `paused_reason='entitlement'`, revived on upgrade; size with a read-only prod query first (ask owner) |
| later | S4-05 | P3 | pre-existing: finite account upgraded to unlimited mid-run gets 0 (`GREATEST(0, -1 - base)`) |
| owner | S3-04 / S3-06 | P1 | IP rate limiting dead in prod; needs Cloudflare as sole ingress (re-verified live: Railway origin still answers 200 without CF-RAY). No HSTS (S3-32). |

## Owner actions pending
- Review and merge #374 (merge = deploy). On the first dispatcher tick after deploy, queued rows of Starter/ended
  accounts are withdrawn (intended).
- Add `SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE=25` to `.env.example` (a permission rule blocked the session from reading
  that file; the code default is 25, so Railway needs nothing unless a different value is wanted).
- Approve 4b. Approve (or not) a read-only prod query to size S4-04. Cloudflare sole ingress for S3-04.

## Failed attempts and traps hit (do not repeat)
- **Backgrounded full suite + TaskStop does NOT kill it on Windows.** The bash loop and its pytest children kept
  running, wrote to the same `_test` DB, and caused phantom failures ("rows vanish mid-test", FK violations,
  rate-limit tests getting no 429 / 401). Run the suite in the FOREGROUND, one batch per call:
  `bash .unlazy/secaudit4/suite-batch.sh init` then `... 0` through `... 5` (each under 10 min). If you must kill,
  find processes by command line (`Get-CimInstance Win32_Process`), and only kill ones whose first test file matches
  one of YOUR `.unlazy/secaudit4/suite/batch_0N` files: other sessions run pytest on the same box.
- **Never edit the worktree while a suite runs on it**; results become a mix of old and new code.
- The shared local Postgres (`C:/Users/Windows/bl-testenv`) crashed once with `0xC0000409` (recurring, 5x in its
  log); it self-recovers in ~3 min. Wait for "ready to accept connections".
- Test env: `source .unlazy/secaudit4/testenv.sh` (own DB `bridgeleads_secaudit4_test`, Redis db 13, venv
  `C:/Users/Windows/bl-rescat-venv`). After the #370/#373 rebase the guard also wants
  `export TEST_DATABASE_URL_MIGRATE=$TEST_DATABASE_URL_SYNC`. NEVER run bare pytest (prod `.env` wipe history).
- A timing test first passed under its mutation because the thread's DB connect through the 6543 proxy takes ~1 s;
  margins widened to 4 s / 6 s. Mutation-check every new test.
- `sed -i` on test files: fine (index and worktree are LF); the CRLF warning is the global autocrlf setting.
- Codex invocation that works here: run inside `bl-wt-secaudit4-codex`, apply the patch there, prompt starts
  "Do NOT load any skill, do NOT run /graphify or any preamble", forbid tests/DB/git/file edits,
  `-c 'mcp_servers={}' --skip-git-repo-check < /dev/null`, background it, read the last "codex" block of the log.
- Codex severity rule (`.claude/rules/codex-collaboration.md`): on disagreement Codex wins unless the docs say
  otherwise; pre-existing issues it finds get logged as new findings rather than widening the PR.

## Next step (exact)
1. `cd C:/Users/Windows/bl-wt-secaudit4 && git fetch origin && gh pr view 374 --json state,mergedAt`. If #374 is
   merged, create a new branch for 4b from `origin/main` in this worktree; if not, ask the owner whether 4b
   should stack on the PR branch or wait.
2. Phase 4b plan in `tasks/todo-security-audit4.md` (files: `src/api/routes/batches.py`, `src/api/routes/segments.py`,
   `src/workers/tasks_helpers/enrich.py`, one test file), Codex consult, implement, regression tests that fail on
   the old code (21st export in a minute across job+batch+segment routes = 429), full suite in foreground batches,
   ruff, Codex review, PR, stop before merge.
