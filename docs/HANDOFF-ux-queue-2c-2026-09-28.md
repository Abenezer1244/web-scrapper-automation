# HANDOFF: UX audit queue — start item 2c (run-count breakdown) (2026-09-28)

Supersedes `docs/HANDOFF-ux-queue-2b-ii-2026-09-27.md` for "where you are". That file and
`docs/HANDOFF-ux-queue-2026-09-27.md` still hold older landmines; this one is self-contained.

## Goal (the owner's standing request)
Work the BridgeLeads UX audit follow-ups **one at a time**. For EACH item:
1. Investigate the code (read it; do not guess).
2. Write the plan in `tasks/todo-<item>.md` (facts verified, files, tests, steps).
3. **Consult Codex on the plan until PLAN: GO** (see "Codex" below).
4. **STOP for the owner's explicit confirmation** before building.
5. Build with **real-DB tests, each proven RED on unfixed code**; mutation-check the guards.
6. Full suite (8 parts), then security review (§14, `.claude/rules/security.md`).
7. **Codex diff review on `origin/main...HEAD` (THREE dots) until GATE: PASS.** Any P1 = no-go.
8. Quiesce check, then merge. **Merge IS deploy**: BE `main` -> Railway api/worker/beat (runs
   `alembic upgrade head` on boot); FE `master` -> Vercel.
9. Verify production directly (deployed SHA, logs, and the owner's logged-in Chrome tab).
10. Append a `docs/BUILD_JOURNAL.md` entry (newest on top) at the end; record failures honestly.
Audit source: FE repo `C:/Users/Windows/bl-wt/fe-ux-audit/docs/ux-audit/UX-AUDIT.md` (49
findings) and `phase-3.0-contracts.md` (Q1–Q6). Project rules: `CLAUDE.md` + `.claude/rules/*`.
UI copy rule: no em dashes.

## Where you are
- **Repo:** backend `web-scrapper-automation`. **Worktree:** `C:/Users/Windows/bl-wt/eligibility`.
- **Branch:** `feat/run-count-breakdown-2c`, fresh from `origin/main` `c0b09b7a`; it contains only
  this handoff. Nothing for 2c is investigated or written yet.
- **FE worktree:** `C:/Users/Windows/bl-wt/fe-session` (has node_modules). It sits on the merged
  branch `feat/run-refusal-402-fe`; for FE work run `git fetch` and branch from `origin/master`
  (`a5ed32a` or later).
- **Test DB:** `bridgeleads_eligibility_test`, at migration **105** (= repo head). Env:
  `source C:/Users/Windows/bl-testenv/env-eligibility.sh` (sets `$PY`, Redis db 7). **Never run
  bare pytest** (the repo `.env` points at PRODUCTION).
- **Prod:** alembic 105; `ENTITLEMENT_ENFORCEMENT=true`; 7 users, 0 hold an API key.

## Queue status
| # | Item | Status |
|---|---|---|
| 1 | F-042/F-041 session reads | ✅ LIVE (FE #164) |
| 2a | F-003/Q5 one active run per scraper | ✅ LIVE (BE #367 mig 104, FE #165) |
| 2b-i | Q6 account `run_eligibility` on `/billing/usage` | ✅ LIVE (BE #369, FE #166) |
| 2b-ii A | per-scraper `run_eligibility`, POST /jobs through one evaluator | ✅ LIVE (BE #375 `ae351c4e`, FE #167 `54bc200`) |
| 2b-ii B | additive 402 `code` + `resumes_at`; toast "Update payment"/"Resubscribe"/"Upgrade plan" | ✅ LIVE (BE #380 `405ba52c`, FE #168 `a5ed32a`) |
| **2c** | **Q1 (F-001): run-count breakdown, done-time snapshot (MIGRATION)** | **NEXT, not started** |
| 2d | Q2 (F-006): `already_delivered` on JobResponse (builds on 2c's snapshot) | queued |
| 2e | Q4 (F-009): FE "Lookup failed" vs "not available" (FE only) | queued |
| 3 | UX batches B–E incl. F-045..F-050 (`UX-AUDIT.md` → Prioritization) | queued |
| 4 | F-043 Phase 2 (= Q3): zero-child batch runs `skipped`, batch PATCH/DELETE, purge script | queued |
| 5 | Phase 4 re-test, Phase 5 `/design-review` + Codex independent review | queued |
Journal entries for all of 2b: `docs/BUILD_JOURNAL.md` 2026-09-28 (two entries; the newer one
corrects a wrong cause in the older one).

## Item 2c — what to build (from the contract; VERIFY every line reference, they are from 2026-09-26)
Spec: FE `docs/ux-audit/phase-3.0-contracts.md` "Q1 (F-001)" (lines 9-72) and the summary table
(line ~268). Problem: a finished run shows four counts that disagree (e.g. records_found 265, the
worker's 12 new + 252 dups = 264, the results page's total_scraped 258 = 246 + 12). Each is
computed at a different pipeline point over a different row set; nothing names the rows between.
- `records_found`: `src/workers/tasks.py` `_set_progress(records_found=len(records))`, BEFORE the
  probate living-TOD filter and BEFORE insert.
- Insert merges rows sharing `source_fingerprint` (`on_conflict_do_nothing` on
  `(job_id, source_fingerprint)`), uncounted.
- Worker `dup_count`: every persisted row with a `dedup_hash`, address or not.
- `record_count` (billed "new leads"): persisted, non-duplicate, ACTIONABLE
  (`src/api/lead_actionability.py`) rows.
- API `total_scraped` / `duplicate_count` / `new_count` / `already_delivered_count`
  (`src/api/routes/jobs.py`, results aggregate): actionable, not `superseded`, read LIVE.
  `ResultsPage.total_scraped`'s docstring ("all records before dedup") is WRONG.
- `stage_label` "Complete: 12 records" prints `record_count` (new leads) as "records".
- Post-completion backfills (e.g. `src/workers/mailing_recovery.py`, selects `status='done'`) move
  rows into "actionable" later, so live counts drift from the frozen ones.
Proposed contract (owner has NOT approved it yet; it goes in the plan):
```
records_found = dropped_before_save + no_address + same_run_merged
              + already_delivered + over_quota + new        (new = record_count = billed)
```
Mutually exclusive, summing to `records_found`, **snapshotted at the done-CAS in the same
transaction as billing**; live twins exposed separately. Precedence: no_address > duplicate
buckets > over_quota. `already_delivered` = `is_duplicate AND coalesce(duplicate_reason,
'prior_run')='prior_run'` (`src/api/results_category.py`), i.e. the same set as prior-run dups.
API: `JobResponse` + `ResultsPage` gain `breakdown` (6 fields) + `breakdown_basis:
"snapshot"|"live"`; DONE label -> "Complete: N new leads"; fix the `total_scraped` doc; the worker's
final log line built from the same snapshot. Storage: columns or a JSON column on `jobs` =
**a migration** (106).
Open questions to put to the owner in the plan: snapshot vs live as the headline after a
backfill (proposal: snapshot headline, live in tab badges); does `superseded` need its own
bucket; how common are fingerprint merges (decides whether `dropped_before_save` splits by
reason). Measure first, read-only, on prod: a GROUP BY of `results` for a real job by
`is_duplicate, duplicate_reason, actionable` vs its `records_found`.
Migration rules: quiesce before merging is MANDATORY; build via `scripts/migrate.py` (advisory
lock), never bare alembic against prod; old jobs have no snapshot (nullable; `breakdown_basis`
says which). Expect the FE follow-up PR (types regen + the breakdown on the run/results pages).

## Next step (exact)
1. Read this file, then `CLAUDE.md`, the Q1 section of the contracts, and memory
   `project_ux_audit_2026_09_26` + `MEMORY.md` landmines (migration / deploy ones especially:
   `merging_to_main_is_deploying`, `migration_advisory_lock`, `backfill_blocks_migration`,
   `concurrent_deploys_kill_long_jobs`).
2. Investigate 2c on this branch: re-verify every line above in today's code (tasks.py moves
   often), trace the done-CAS/billing transaction, and run the read-only prod GROUP BY for one or
   two recent jobs (counts only, no PII).
3. Write `tasks/todo-run-count-breakdown-2c.md`; Codex until PLAN: GO; **stop for the owner**
   (include the open questions and the migration plan).

## How things were done this session (reuse; all verified working)
- **Codex:** prompts > ~32 KB go through stdin, from an empty scratch dir:
  `codex exec - -c 'mcp_servers={}' --skip-git-repo-check -s read-only < prompt.txt > out.txt`.
  The verdict is after the LAST line equal to `codex`. Say explicitly "this is a PLAN review; the
  code shown is today's, context only" or it reports "not implemented" as P1. **Never run
  `codex review`** (it runs pytest on your test DB). Diff reviews: `git diff origin/main...HEAD`.
- **Full suite:** `ls tests/test_*.py | sort > all.txt; split -n l/8 all.txt part_` (split needs a
  FILE, not stdin), run each part with `"$PY" -m pytest $(cat part_xx) -q -p no:cacheprovider`.
  Run parts **backgrounded and awaited ONE AT A TIME**: a foreground part over 600 s moves to the
  background and the next part then shares the DB. Read the real exit code (never `| tail`).
- **Quiesce (read-only), from the OneDrive repo dir (it is railway-linked, worktrees are not):**
  `railway run --service worker C:/Users/Windows/bl-rescat-venv/Scripts/python.exe
  C:/Users/Windows/bl-checks/quiet.py` (all zeros) + a read-only query of non-terminal `jobs`,
  `batch_runs` in pending/running and `alembic_version` (`DATABASE_URL_MIGRATE`,
  `set_session(readonly=True)`). Merge only if quiet: `gh pr merge N --squash
  --match-head-commit <full sha>` (never `--admin`). Verify with `railway status --json` (commit
  hash on api/worker/beat) + `curl https://api.bridgeleads.io/health` + `railway logs --service
  api`.
- **OpenAPI:** `C:/Users/Windows/bl-rescat-venv/Scripts/python.exe scripts/export_openapi.py`
  then `--check`, plus a structural JSON diff vs `git show origin/main:schema/openapi.json`.
  `.venv-schema` is dead. Prod does not serve `/openapi.json`.
- **FE types:** `git -C <BE> show <sha>:schema/openapi.json > tmp; npx --no-install
  openapi-typescript tmp -o lib/api-types.generated.ts` (`npm run gen:api-types` 404s: private
  repo). The FE CI drift gate compares against BE `main`: a BE schema change turns EVERY FE PR
  red until the FE regenerates.
- **FE proof (no test runner):** scratchpad stub API + Playwright against the real FE. Stubs from
  this session (session `5ab04918` scratchpad): `elig_stub.mjs`/`elig_verify.mjs`,
  `refusal_stub.mjs`/`refusal_verify.mjs`. Recipe: `.env.local` in the FE worktree
  (`NEXT_PUBLIC_API_URL=http://127.0.0.1:8123`, random `AUTH_SECRET`, `AUTH_URL`/`NEXTAUTH_URL`
  `http://127.0.0.1:3111`, `AUTH_TRUST_HOST=true`), `npm run build`, `npx next start -p 3111 -H
  127.0.0.1`; Chromium `C:/Users/Windows/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe`
  via `file:///C:/Users/Windows/node_modules/playwright-core/index.js`. Kill servers by port
  (`netstat -ano | grep :3111`), delete `.env.local`, rebuild without it at the end.
- **Prod FE check:** the owner's Chrome tab (claude-in-chrome) is logged in to bridgeleads.io;
  never type credentials (ask the owner to sign in + MFA if it expired). In-page, the bearer is in
  `/api/auth/session` (as `lib/api.ts` reads it); call the API in-page and return COUNTS only.

## Failed attempts / landmines hit (don't repeat)
- 🛑 `test_rls_isolation` "permission denied for table results / delivered_records" is the test
  DB losing grants (roles are cluster-scoped, grants per-DB; the fixture grants only on role
  creation). It hit an EXISTING DB this session. Check `has_table_privilege('bridgeleads_rls_test',
  'results','SELECT')`; if false, `GRANT USAGE ON SCHEMA public` + `GRANT SELECT, INSERT, UPDATE,
  DELETE ON ALL TABLES IN SCHEMA public` + `GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public`
  TO `bridgeleads_rls_test` (test DB only), then re-run the whole part.
- 🛑 I blamed a dashboard crash on a failed `/analytics/summary`; it was my stub's
  `/auth/onboarding` missing `next_action`. Find the failing property in the bundle before naming
  a cause. A stub `/auth/onboarding` must include `next_action`.
- 🛑 TanStack Query v5: a failed BACKGROUND refetch sets `isError` while data stays, so
  `isError ? <ErrorState>` blanks a cached page (fixed on /scrapers in FE #167).
- 🛑 Claude Code's permission classifier errored on every Bash/Edit for a while (10 in a row ends
  the turn). Stop, leave work on disk, report, resume later.
- 🛑 A Python patch writing `"\\n"` into a JS file produced a literal newline. Edit JS with the
  Edit tool.
- An Auth.js "Failed to fetch" console error right after login in the stub rig is a pre-existing
  navigation race (A/B: also on unmodified master); not a regression.
- `main` moves often (the skip-trace workstream merged #376, #379 mig 105, #382 today): fetch
  before every review and merge; reviews that predate a rebase don't cover it.
- Test connectors: use the conftest `connectors` fixture (moved there in #380); the `db` teardown
  does not delete connectors.

## Follow-ups queued (not in 2c)
`connector_unavailable` refusal (gate + page refuse a run no active connector can serve);
`jobs.was_ai` snapshot (migration) so the monthly AI count can't change retroactively; `/scrapers`
long record-type label overlaps the record count at ~1300 px; make the `test_rls_isolation` role
fixture GRANT on every run.
