# HANDOFF: UX audit queue after 2c (2026-09-30)

Supersedes `docs/HANDOFF-ux-queue-2c-bis-2026-09-28.md`. Self-contained: read this first.

## Goal (owner's standing request)
Work the BridgeLeads UX-audit follow-ups ONE AT A TIME, each end to end:
investigate -> plan in `tasks/todo-<item>.md` -> Codex until PLAN: GO -> owner confirms ->
build with real-DB tests proven RED on unfixed code -> full suite in 8 parts -> security
review x2 -> Codex diff review on `origin/main...HEAD` (THREE dots) until GATE: PASS (any
P1 = no-go) -> quiet check -> merge (merge IS deploy) -> verify prod -> FE PR if the API
changed -> BUILD_JOURNAL entry. UI copy: no em dashes. Owner said "do all yourself" and, for
merges/deploys, "I give you permission" (see "Deploy permission" below).

Audit + ledgers live in the FE repo `docs/ux-audit/` (UX-AUDIT.md, 49 findings;
`phase-3.0-contracts.md` has the per-question contracts Q1-Q6).

## Where you are (nothing is in flight)
Everything from the previous queue items is MERGED and LIVE. There is no open feature branch.
| Item | PR | Squash SHA | Live |
|---|---|---|---|
| 2c-bis attempt fence | BE #390 | `fd200257` | 2026-09-30 09:11Z |
| 2c run-count breakdown (migration 106) | BE #394 | `8ea43860` | 2026-09-30 11:31Z |
| 2c FE (render the breakdown) | FE #169 | `b4dbb12d` | Vercel 12:05Z |
| Build journal entry | BE #396 | `d1003700` | merged (redeploy healthy) |
This handoff sits on branch `docs/handoff-ux-queue-2d-2026-09-30` (pushed, NOT merged: a
docs-only merge still redeploys Railway; merge it only with the next real PR or when quiet).

## Next steps (exact)
1. **Close out 2c: the first real run's snapshot.** No job had finished after 2c went live
   (checked 12:15Z). Run the read-only check (it filters on `finished_at > 11:31:16Z`):
   `cd "C:/Users/Windows/OneDrive - Seattle Colleges/Desktop/web-scrapper-automation"`
   `railway run --service worker C:/Users/Windows/bl-rescat-venv/Scripts/python.exe <script>`
   where `<script>` is `first_snap.py` below. Expect per job: the six `breakdown_*` sum to
   `records_found`, and `breakdown_new == record_count == billed_count`; or all six NULL with
   `retry_count > 0` (retried runs freeze nothing, by design). A MISMATCH is a bug: stop and
   investigate (`src/api/run_breakdown.py`, `src/workers/tasks_helpers/finalize.py`).
2. **Start 2d = Q2 (F-006): "does GET /jobs carry already_delivered_count?"** Contract in FE
   `docs/ux-audit/phase-3.0-contracts.md` "Q2". IMPORTANT, 2c already did half of it:
   `JobResponse.breakdown.already_delivered` (snapshot) is now on every job that froze one.
   What remains to decide/plan: (a) the live fallback for jobs WITHOUT a snapshot (every job
   finished before 2026-09-30 11:31Z, and retried runs): one grouped aggregate in `list_jobs`
   using `already_delivered_condition()`, `actionable_condition()`, `tax_cap_condition()`,
   scoped by `user_id` and `job_id = ANY(:ids)`, on the RLS session; or decide the snapshot is
   enough; (b) where the FE jobs list shows it; (c) API doc: scope is ACCOUNT-WIDE (dedup_hash
   has no county/record type). Make a fresh worktree from `origin/main`
   (`git worktree add -b feat/<name> C:/Users/Windows/bl-wt/<dir> origin/main`), write
   `tasks/todo-2d-<name>.md`, brainstorm, Codex PLAN review, then STOP for owner confirmation.
3. Later queue (from memory `project_ux_audit_2026_09_26`): 2e Q4 "Lookup failed"; item 3
   batches B-E (+F-045..F-050); item 4 F-043 Phase 2; item 5 Phase 4/5.

## What 2c-bis + 2c changed (so you know the code you are standing on)
- `src/workers/tasks_helpers/finalize.py`: `finalize_billing_and_done` (billing + done-CAS,
  fenced on `AttemptToken`; outcomes DONE / ALREADY_TERMINAL / BILLING_FAILED /
  LOST_OWNERSHIP; `FinalizeOutcome.frozen` = the frozen breakdown), `_terminal_cleanup`,
  `release_run_claims_if_owned`. Billing reads `read_partition(...).new`.
- `src/workers/tasks_helpers/status.py`: `AttemptToken(started_at, retry_count)`,
  `claim_attempt`, `attempt_state` (FOR UPDATE), `finalize_exit`; every attempt-scoped writer
  accepts the token (a bare datetime is the legacy test form).
- `src/workers/tasks.py` `run_scrape_job`: `attempt_token` everywhere; `_after_missed_write`
  (one terminal-vs-lost decision), `_still_ours`, `_fail_attempt` (the only `_fail_job` call);
  completion line = `completion_message(display_count, _outcome.frozen)`.
- `src/workers/tasks_helpers/enrich.py` `_enqueue_skip_trace_rows(..., attempt_token=)`:
  ownership checked right after `lock_job_for_claim`; lost/terminal -> nothing queued.
- `src/api/run_breakdown.py`: the ONE partition (`partition_case`), `decide_snapshot` /
  `snapshot_columns` (token = both parts), `breakdown_from_job`, `live_breakdown`.
- `src/api/routes/jobs.py`: `JobResponse.breakdown` (snapshot only); `ResultsPage.breakdown`
  (snapshot, else live for a done job; a REJECTED snapshot shows nothing).
- Migration `alembic/versions/106_jobs_run_breakdown.py`. Prod alembic head = **106**.
- Tests: `tests/test_finalize.py`, `tests/test_finalize_fence.py`, `tests/test_run_breakdown*.py`
  (incl. the in-suite mutation harness `test_run_breakdown_mutations.py`).
- FE (bridgeleads-web): `components/run-breakdown.tsx`, results + live pages, `lib/types.ts`
  aliases `RunBreakdown` / `BreakdownBasis`, regenerated `lib/api-types.generated.ts`.

## Environment / reusable commands
- Test env: `source C:/Users/Windows/bl-testenv/env-eligibility.sh` (sets `$PY`, DB
  `bridgeleads_eligibility_test`, Redis db 7). **Never bare pytest** (the repo `.env` is PROD).
  The test DB is now at migration **106**.
- Full suite, 8 parts, ONE AT A TIME (RAM is tight): from the worktree
  `ls tests/test_*.py | sort > all.txt; split -n l/8 all.txt part_`, then per part
  `"$PY" -m pytest $(cat part_xx) -q -p no:cacheprovider > part_xx.log 2>&1; echo $? > part_xx.exit`,
  backgrounded, one per background job. Read the real exit code, never through `| tail`.
- Codex (never `codex review`, it runs pytest on your DB): build a prompt file (preamble +
  `git diff origin/main...HEAD`), then from an empty dir:
  `codex exec - -c 'mcp_servers={}' --skip-git-repo-check -s read-only -C <worktree> < prompt.txt > out.txt`.
  Verdict = text after the LAST line equal to `codex`. Say "do not try to run tests".
- OpenAPI: `"$PY" scripts/export_openapi.py --check` (regenerate without `--check`).
- FE types: `git -C <BE worktree> show origin/main:schema/openapi.json > tmp.json;`
  `npx --no-install openapi-typescript tmp.json -o lib/api-types.generated.ts` (FE CI's drift
  gate compares against BE main, so an API change needs this FE PR right after).
- FE worktree with deps: `C:/Users/Windows/bl-fe-breakdown` (branch merged; reuse or make a
  new one from `origin/master` + `npm ci`). The main FE checkout is on someone else's branch.
- FE proof without a test runner: stub API + Playwright. Scripts of session 70567250:
  `.../70567250-0441-4a8b-b4e5-29e6e98428ef/scratchpad/fe2c/bd_stub.mjs` and `bd_verify.mjs`.
  `.env.local` (gitignored) = `NEXT_PUBLIC_API_URL=http://127.0.0.1:8123`, random
  `AUTH_SECRET`, `AUTH_URL`/`NEXTAUTH_URL=http://127.0.0.1:3111`, `AUTH_TRUST_HOST=true`;
  `npm run build`; `npx next start -p 3111 -H 127.0.0.1`. Kill only YOUR processes by port
  (check the command line), delete `.env.local` afterwards.
- Quiet check before ANY merge (merge = deploy; Railway api/worker/beat run migrations on
  boot): from the OneDrive repo dir (it is railway-linked; worktrees are not)
  `railway run --service worker C:/Users/Windows/bl-rescat-venv/Scripts/python.exe C:/Users/Windows/bl-checks/quiet.py`
  -> all four counts 0. Then `gh pr merge N --squash --match-head-commit "$(git rev-parse HEAD)"`
  (never `--admin`; branch protection requires the branch up to date with main).
- Verify a deploy: `railway status --json` (commitHash on api/worker/beat = merge SHA),
  `curl https://api.bridgeleads.io/health`, `railway logs --service <svc>` for errors.
  Prod alembic head: read `alembic_version` with `DATABASE_URL_MIGRATE` (it reads EMPTY via
  `DATABASE_URL_SYNC`).

`first_snap.py` (read-only, for next step 1):
```python
import os, re, psycopg2
c = psycopg2.connect(re.sub(r"^postgresql\+\w+://", "postgresql://", os.environ["DATABASE_URL_SYNC"]), connect_timeout=15)
c.set_session(readonly=True); k = c.cursor()
k.execute("""SELECT id, status, retry_count, records_found, record_count, billed_count,
  breakdown_dropped_before_save, breakdown_no_address, breakdown_same_run_merged,
  breakdown_already_delivered, breakdown_over_quota, breakdown_new
  FROM jobs WHERE finished_at > '2026-09-30 11:31:16+00' ORDER BY finished_at""")
for r in k.fetchall():
    b = r[6:]
    ok = None if b[0] is None else (sum(b) == r[3] and b[5] == r[4] == r[5])
    print(str(r[0])[:8], r[1], "retry", r[2], "found", r[3], "record", r[4], "billed", r[5], b,
          "reconciles" if ok else ("NULL snapshot" if ok is None else "MISMATCH"))
c.close()
```

## Deploy permission
The Claude Code auto-mode classifier blocks `gh pr merge` and `railway` scale/deploy as
"Production Deploy". The owner's "do all yourself" was NOT enough; it went through after the
owner wrote "Just deploy and merge yourself i give you permission". If blocked, stop and ask;
do not route around it. `railway scale` is BROKEN in CLI 4.33 (GraphQL `railwayMetal` panic)
and `railway upgrade` needs an interactive shell, so the worker cannot be scaled to 0 from
here: merge only when `quiet.py` reads all zeros immediately before the merge.

## Failed attempts / landmines (don't repeat)
- 🛑 NEVER hand-type a full SHA. I padded short SHAs with invented hex twice (a
  `--match-head-commit` and a `--force-with-lease`); both were refused. Use `git rev-parse`.
- 🛑 Low RAM: Claude Code reaps background jobs; a reaped pytest/codex child SURVIVES. Find it
  (`Win32_Process` command line), confirm it is yours, wait on its PID. Other sessions run
  codex on this box: never kill theirs. Under pressure `test_db_safety` fails 17 tests with
  0xC0000142 (subprocess could not start): re-run that file alone (42/42).
- 🛑 After a harness "stopped for low memory", do not restart on your own; ask the owner.
- 🛑 main moves often (4 merges during 2c). After each rebase prove the branch patch is
  unchanged: `diff <(git diff <old-base> <reviewed-sha>) <(git diff origin/main HEAD)` = 0
  lines, plus `export_openapi.py --check` when schemas/routes are touched.
- 🛑 Railway can serve "Application not found" for the api (custom AND up.railway.app) with the
  deployment SUCCESS; on 09-30 it lasted ~3h and self-recovered. Check both domains + logs
  before blaming a deploy.
- 🛑 `tests/test_contact_lookup_quote.py::test_a_quote_redis_cannot_store_is_never_shown` needs
  Redis ACLs or `BL_TEST_REDIS_SERVER`: it fails locally for that reason only (green in CI).
- 🛑 FE stub `/auth/onboarding` must return a full `next_action` object or the dashboard crashes
  ("reading 'title'"). `AnimatedCounter` text is "0 1 2 ... 9" per digit: read the visible slot.
  Auth.js "Failed to fetch" can appear once on the first navigation after login (harness timing).
- 🛑 After every rebase `grep -n attempt_started_at src/` (a clean rebase once left a NameError).
- 🛑 Python patch scripts over CRLF files: anchor with `\r\n`; non-ASCII (em dash) in a heredoc
  script broke an anchor once: use the Edit tool for those lines.
- 🛑 After each commit, `git status --short | grep -v '^??'` must be empty.

## Queued follow-ups (owner-visible, not started)
Stale-attempt log-publish races (owner-accepted P2s); attempt-unique export key; widen the
fence to plan-cap/enrichment writes; `connector_unavailable` refusal; `jobs.was_ai` snapshot;
`/scrapers` label overlap; `test_rls_isolation` role GRANTs; `AnimatedCounter` screen-reader
value (FE); Railway CLI upgrade.
