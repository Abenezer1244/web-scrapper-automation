# HANDOFF: UX audit queue, resume at item 2b (2026-09-27)

## Goal
Owner's request (verbatim intent): continue the BridgeLeads UX audit follow-ups **one at a time**.
For EACH item:
1. Investigate the code.
2. Write a plan in `tasks/todo-*.md`.
3. **Consult Codex on the plan.**
4. **Stop for the owner's explicit confirmation.**
5. Build it with real-DB tests, and prove each test fails on unfixed code.
6. Run the full suite, then a security review.
7. **Codex diff review until GATE: PASS.** Any P1 is a no-go.
8. Quiesce check, then merge. Merging deploys: backend `main` goes to Railway, frontend `master` goes to Vercel.
9. Verify production directly, not by assuming the deploy worked.

Project rules: `CLAUDE.md`, and `.claude/rules/*`, which are loaded automatically. The UX audit lives in the FE repo: `C:/Users/Windows/bl-wt/fe-ux-audit/docs/ux-audit/UX-AUDIT.md` (49 findings) and `phase-3.0-contracts.md` (Q1–Q6).

## Where you are
- **Repo:** backend `web-scrapper-automation`.
- **Worktree:** `C:/Users/Windows/bl-wt/eligibility`.
- **Branch:** `feat/run-eligibility`, from `origin/main` `6194d73c`.
- **Code written so far:** none. Only this handoff exists on the branch.
- **Status of item 2b:** investigation started. Next step is to write the plan.

## Queue
Items 1 and 2a are DONE and LIVE. Resume at **2b**.

| # | Item | Status |
|---|---|---|
| 1 | F-042/F-041: session-read storm and dashboard spinner | ✅ FE #164 `6030491`, verified in prod (idle session req/min 52 → 1) |
| 2a | F-003 / Q5: one active run per scraper | ✅ BE #367 `6194d73c` (migration **104**, index valid in prod) + FE #165 `dfadf4d` |
| **2b** | **Q6 run_eligibility, split in two** | **NEXT, see below** |
| 2c | Q1: run-count breakdown so found = new + already delivered + … (needs a migration) | queued |
| 2d | Q2: `already_delivered` on JobResponse | queued |
| 2e | Q4: FE shows "Lookup failed" as distinct from "not available" | queued |
| 3 | Batches B–E, including F-045..F-050 (see `UX-AUDIT.md` → Prioritization) | queued |
| 4 | F-043 Phase 2: zero-child runs recorded as `skipped` (not `done`), batch pause/delete endpoints, prod rows deleted while paused (use the `scraper_deleted` audit log) | queued |
| 5 | Phase 4 re-test, Phase 5 `/design-review` and the Codex independent review | queued |

### 2b plan outline (not yet written or Codex-consulted)
Full spec: `phase-3.0-contracts.md` Q6, lines 223–265.

**2b-i: the account-level rule.** Refactor `quota_block_reason(user, now)` (`src/api/quota.py:130-170`) into `run_eligibility(user, now)`, returning `{can_run, code, message, resumes_at}`:

| `code` | `resumes_at` |
|---|---|
| `frozen` | null |
| `ended` | null |
| `over_limit` | the window end, `effective_window(user, now)[1]` |

Then:
- Keep `quota_block_reason` as a thin wrapper, so its callers are unchanged:
  - `jobs.py` enqueue
  - `batches.py:~273`
  - `dispatch.py` (scheduler, and batch dispatch)
  - `batch_tasks.py`
- `GET /billing/usage` (`src/api/routes/billing.py:~652-700`):
  - add `run_eligibility`
  - report the **effective** limit, via `effective_records_limit`. Today it returns the raw `records_limit`, which disagrees with the gate across a boundary that carries a pending downgrade.
- Update the schemas, then regenerate `schema/openapi.json` (memory: `reference_openapi_regen_env_matters`). The FE types are regenerated in a follow-up FE PR, because the FE CI drift gate compares against the BE `main` schema.

**2b-ii (later, separate step):** per-config eligibility on the scraper response (`not_entitled`, `config_inactive`, `ai_limit`, `run_in_flight`). Also structured 402 bodies; the FE `readErrorBody` already handles an object `detail`.

**Open question from Q6:** the production value of `ENTITLEMENT_ENFORCEMENT`. It's unverified, and the code default is NOT the prod value (memory landmine). If it's off, `not_entitled` should be a warning, not a block.

## Changes made this session (all merged)
- **FE #162:** UX Batch A.
- **FE #163:** API types regenerated after BE #360 broke the drift gate.
- **FE #164:** session reads. `readSession()` is cached, single-flight, 30 s, generation-guarded and never broadcasts. `readFreshSession()` is used for the refresh and sign-out paths. Once a sign-out is under way in any tab, no request is sent. `openLogStream()` keeps the bearer inside `lib/api.ts`. The shell renders on the server's session verdict.
- **FE #165:** a 409 `run_in_flight` opens `/live/{id}`, but only if that job is actually running.
- **BE #363:** F-043.
  - Deleted batch children are no longer scraped or billed (`current_batch_child_clause`).
  - `DELETE /scrapers` clears `paused_reason`.
  - `run_scrape_job` skips jobs whose scraper is deleted or paused.
- **BE #367:** migration 104, `uq_jobs_one_active_per_config`.
  - `Job.holds_run_slot(now)`: active, OR cancelled after a worker started it, within 5 min. Shared by POST /jobs, the scheduler and the batch fan-out.
  - 409 body: `{code:"run_in_flight", job_id, message}`.
- **BE #362:** journal entry. Nothing yet in the journal for #363–#367 beyond the F-043 note; **append an entry at the end of the next session.**

## Failed attempts, landmines hit (don't repeat)
- **Codex prompts over ~32 KB fail with exit 126.** Pipe via stdin: `codex exec - ... < file`. Always run it from an empty scratch dir with `-c 'mcp_servers={}' --skip-git-repo-check`. The final verdict can be after the first `codex` block: read the whole output file.
- **Async SQLAlchemy:** `rollback()` expires every ORM object, and reading `config.id` afterwards is a MissingGreenlet 500. Capture plain ids BEFORE a flush that may be rolled back.
- **Adding a unique index breaks existing tests that built 2 active jobs on 1 config.** Give each concurrent job its own config; claims and quota are account-wide.
- **Shell `node -e` / `sed` with backslashes corrupted regexes** (a literal backspace byte). Edit through written files or the Edit tool.
- **Memory pressure reaps background tasks:** the headed browser, `next start` and watchers. `TaskStop` on `npx next start` leaves node holding port 3100 with the OLD build. Stop the real PID via `Get-NetTCPConnection -LocalPort 3100`.
- **The full pytest suite exceeds a 590 s timeout.** Split it into 8 parts (`split -n l/8`) in the foreground. Use an isolated DB only.
- **`gh pr checks` shows "pending 0" for a running job.** Check `gh api .../actions/jobs/<id>` instead.
- **`sleep N; cmd` in the foreground is blocked.** Use until-loops.
- **Pre-merge quiesce** (read-only, via `railway run` + `DATABASE_URL_MIGRATE`): scratchpad scripts `inflight_check.py` and `q5_prod_check.py`. They're easy to recreate: query non-terminal jobs, active batch_runs, and for a new index, duplicate violations.

## Test environment
- **Isolated DBs:** `bridgeleads_runguard_test`, env script `C:/Users/Windows/bl-testenv/env-runguard.sh`. Never use bare pytest.
- **For 2b**, create `bridgeleads_eligibility_test` and copy the env script with a new Redis db index (0–14; 8, 9 and 13 are in use), then:
  ```
  source env && "$PY" -m alembic upgrade head
  ```
- **Python:** `C:/Users/Windows/bl-rescat-venv/Scripts/python.exe` (ruff 0.15.6 as in CI).
- **FE worktree:** `C:/Users/Windows/bl-wt/fe-session`. It has node_modules; branch it from `origin/master` for FE work.
- **Browser verification rig** (FE changes against the prod API): memory `reference_prod_ux_capture_rig`. `headed2.mjs` and the saved `AUTH_SECRET` are in the old scratchpad and may be gone. Recreate them from that memory. The owner must log in with MFA on both tabs.

## Next step
1. In `C:/Users/Windows/bl-wt/eligibility`, read `src/api/quota.py` (window helpers, `effective_records_limit`) and the `billing.py` usage route plus its schema.
2. Write `tasks/todo-run-eligibility.md` for **2b-i**, consult Codex (stdin), then **stop for the owner's confirmation**.
