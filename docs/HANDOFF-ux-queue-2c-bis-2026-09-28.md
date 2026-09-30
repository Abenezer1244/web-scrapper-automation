# HANDOFF: UX queue 2c-bis (finalization attempt fence) + 2c (run-count breakdown) (2026-09-28)

Supersedes `docs/HANDOFF-ux-queue-2c-2026-09-28.md` for "where you are". Self-contained.

## Goal (owner's standing request)
Work the BridgeLeads UX-audit follow-ups one at a time: investigate, plan in `tasks/todo-<item>.md`,
Codex until PLAN: GO, owner confirms, build with real-DB tests proven RED on unfixed code, full
suite in 8 parts, security review x2, Codex diff review on `origin/main...HEAD` (THREE dots) until
GATE: PASS (any P1 = no-go), quiesce, merge (merge IS deploy: Railway api/worker/beat run
`scripts/migrate.py` on boot), verify prod, BUILD_JOURNAL entry. UI copy: no em dashes.

Current item is **2c = Q1 (F-001) run-count breakdown** (a finished run showed 4 disagreeing
counts). Planning it exposed a pre-existing money defect, so the owner approved (2026-09-28,
accepting every recommendation):
1. Build 2c, then **2c-bis**; **2c-bis must be merged, DEPLOYED and verified before 2c merges**.
2. Deploy runbook, not a new schema gate. Intake pause = **owner scales Railway `worker` to 0**
   before each merge, back to 1 after verification.
3. 2c product answers: snapshot is the headline (live in tab badges); no `superseded` bucket; no
   split of `dropped_before_save`; wording "Not saved / No address / Combined in this run /
   Already delivered / Over your plan limit / New leads".
4. 2c-bis: two Codex P2 "log-publish races" (a stale attempt's log line slipping onto the
   replacement's live stream) ACCEPTED as a documented follow-up.

## The defect 2c-bis fixes
Watchdog re-queue (`scheduler_helpers/health.py`) and `_retry_scrape_job` set `status=pending,
started_at=NULL, retry_count+1`; a replacement attempt B claims. A STALLED (not dead) attempt A
that resumes used to: bill the job (billing CAS keyed only on `billing_applied_at IS NULL`), mark
B's run done (done-CAS had no attempt scope), and, once fenced naively, refund B's quota
reservation (`release_quota_reservation` is not attempt-aware).

## Where you are
### 2c-bis (NEXT, finish this first)
- Worktree `C:/Users/Windows/bl-wt/fence`, branch `feat/finalize-attempt-fence-2c-bis`, head
  `dba68bc8` (pushed), based on main `4f56a5e3`. **main has moved to `fa658ccf`: rebase first.**
- Plan (read its "Review" section first): `tasks/todo-finalize-attempt-fence-2c-bis.md`.
- Codex plan: GO (round 8). Codex diff review: r1-r5 findings fixed; **r6 = GATE FAIL**:
  - **P1 OPEN: paid skip-trace enqueue is not attempt-fenced.** `src/workers/tasks.py` (the
    `_enqueue_skip_trace_rows(db, job, r, job_id, config, on_begin=lambda: _set_stage(... "queuing_contacts", expected_started_at=attempt_token))`
    call, which runs in ENRICHMENT, before finalization) and
    `src/workers/tasks_helpers/enrich.py` ~2491-2753. A stale attempt can write cache/claims and
    commit queued paid lookups for a job B owns. Fix (Codex): after `lock_job_for_claim` (a
    `pg_advisory_xact_lock`), call `attempt_state(db, job_id, user_id, token)` (FOR UPDATE);
    lost/terminal -> rollback, enqueue nothing; hold the lock through the final commit. The
    token must be passed into `_enqueue_skip_trace_rows` (new kwarg). 🛑 memory
    `set_stage_commits_releases_advisory_lock`: NO commit and NO log line between the advisory
    lock and the final commit; `on_begin` must stay BEFORE the lock. Add a RED-first two-session
    test (B re-claims, A enqueues -> zero `pending_skip_trace_rows`, no cache writes).
  - **P2 OPEN: terminal vs lost at early returns.** `_still_ours()` (closure in run_scrape_job)
    and the `_fail_job`-returned-False paths return without cleanup. Terminal (cancelled/failed)
    must run `_terminal_cleanup` after rollback; lost (live replacement) must release nothing.
    Reuse `finalize_exit(attempt_state(...))`. Sites (pre-rebase numbering): `_still_ours`
    ~463-482; fail paths ~1427, 1514-1539, 1567, 1961-1974, 2113-2128.
- Full suite last run: **8/8 green, 5565 passed** at `abcd8f1f`+ (code unchanged since).
- Security review x2: clean.

### 2c (after 2c-bis is LIVE)
- Worktree `C:/Users/Windows/bl-wt/eligibility`, branch `feat/run-count-breakdown-2c`, head
  `3a3fcd05` (pushed), based on main `c0b09b7a`. Plan: `tasks/todo-run-count-breakdown-2c.md`
  (Codex PLAN: GO r8).
- Built: migration **106** (six nullable int `jobs.breakdown_*` columns, `lock_timeout 5s`);
  `src/api/run_breakdown.py` (ONE CASE partition over a job's saved rows; `new` = the billing
  predicate verbatim; `decide_snapshot`, `breakdown_from_job`, `live_breakdown`,
  `completion_message`); worker bills from `partition.new` and freezes the six in the done-CAS;
  `JobResponse`/`ResultsPage` gain `breakdown` + `breakdown_basis`; DONE label "Complete: N new
  leads"; OpenAPI additive only. Tests: `tests/test_run_breakdown*.py` (incl. in-suite mutation
  harness), `tests/test_run_breakdown_api.py` (HTTP + RLS as `bridgeleads_app`).
- Codex diff: r1 fixed; r2 left one P2 (mutation harness) FIXED in `3a3fcd05`, NOT re-reviewed.
  Suite: only parts 1-2 ran (pre-fix); full run needed after the rebase.
- **On rebase onto 2c-bis:** the snapshot decision (`decide_snapshot`, billing via
  `read_partition`) must move INTO `src/workers/tasks_helpers/finalize.py`
  (`finalize_billing_and_done`) because the billing block now lives there; `decide_snapshot`
  should take the `AttemptToken` (owner read = `attempt_state`); T8 in
  `tests/test_run_breakdown_finalize.py` must drive `finalize_billing_and_done` instead of its
  local `_bill`. Expect conflicts in `tasks.py`.

## Files changed by 2c-bis (vs main)
- `src/workers/tasks_helpers/finalize.py` (NEW): `finalize_billing_and_done` (verbatim move of
  the force-finalize guard -> done-CAS commit; commit `b4b731e5` differs only in 3 returns),
  `FinalizeKind` (DONE / ALREADY_TERMINAL / BILLING_FAILED / LOST_OWNERSHIP), `_fenced_exit`,
  `_terminal_cleanup` (reservation + claims), `_settle_user_charge` (verbatim settle statement),
  `release_run_claims_if_owned`, moved `_alert_dedup_release_failed` +
  `_release_claims_of_cancelled_job` (re-exported from tasks.py).
- `src/workers/tasks_helpers/status.py`: `AttemptToken(started_at, retry_count)`,
  `_attempt_parts/_attempt_clauses/_attempt_sql` (bare datetime still accepted = legacy),
  `claim_attempt` (RETURNING retry_count; `claim_job_for_attempt` now wraps it),
  `attempt_state` (FOR UPDATE; owned = token match AND non-terminal), `finalize_exit`; every
  attempt-scoped writer (`_set_status`, `_set_progress`, `_set_stage`, `_fail_job`,
  `_retry_scrape_job`, heartbeat) builds both predicates.
- `src/workers/tasks.py`: `attempt_started_at` -> `attempt_token` everywhere; claim via
  `claim_attempt`; every `_fail_job` and status transition passes the token; `_still_ours`
  gates all 6 direct stage writes; date window = token-scoped UPDATE + `set_committed_value`;
  failure-path claim DELETEs -> `release_run_claims_if_owned`; plan-cap
  `release_capped_dedup_claims` guarded by `attempt_state`; finalization via
  `finalize_billing_and_done`.
- `pyproject.toml`: S608 per-file ignore for finalize.py (moved SQL, reason in comment).
- Tests: `tests/test_finalize.py` (5, behavior-neutral move), `tests/test_finalize_fence.py`
  (race tests with two sessions, forced timestamp collision, billing-failed branch via
  `_settle_user_charge` injection, wiring tests via balanced `_calls()` parser).

## Environment / reusable commands
- Test env: `source C:/Users/Windows/bl-testenv/env-eligibility.sh` (sets `$PY`, DB
  `bridgeleads_eligibility_test`, Redis db 7). **Never bare pytest** (repo `.env` = PROD).
- 🛑 Test DB is at migration **105** (downgraded for 2c-bis, which has no 106). For 2c: from the
  eligibility worktree `unset DATABASE_URL_MIGRATE; "$PY" scripts/migrate.py` (re-applies 106).
  Down again: `"$PY" -m alembic downgrade 105` from the eligibility worktree.
- Suite runner (8 parts): scratchpad of session 67fa093f `.../scratchpad/suite_bis/run_part.sh`
  + `part_aa..ah` (regenerate: `ls tests/test_*.py | sort > all.txt; split -n l/8 all.txt part_`).
  Run parts ONE AT A TIME, backgrounded; read `partX.exit`. Use `rm -f "${S:?}/${p:?}.exit"`
  (the safety hook blocks `rm` on bare `$S/$p`).
- Codex: `cd <empty dir>; codex exec - -c 'mcp_servers={}' --skip-git-repo-check -s read-only < prompt.txt > out.txt`;
  verdict after the LAST line equal to `codex`. Never `codex review`. Diff = `git diff origin/main...HEAD`.
  Tell it "do not try to run tests".
- Quiesce / merge / verify: see `docs/HANDOFF-ux-queue-2c-2026-09-28.md` "How things were done"
  (quiet.py via `railway run --service worker` from the OneDrive repo dir; `gh pr merge N --squash
  --match-head-commit <sha>`, never `--admin`; `railway status --json`, `/health`, logs).
- Prod read-only measurement script: `C:/Users/Windows/bl-checks/breakdown_2c.py`.

## Failed attempts / landmines hit this session (don't repeat)
- 🛑 **Low RAM:** Claude Code reaps background jobs when memory is low; the reaped **pytest /
  codex child SURVIVES** its shell. Find it (`Win32_Process` command line), confirm it is yours
  (file list + parent), then kill or wait for it (a surviving codex still writes its output).
  Other sessions run pytest/codex on this box too: never kill theirs. One heavy job at a time.
- 🛑 A clean rebase left a stale `attempt_started_at` (NameError at runtime) from main's new
  `run_eligibility` fail: after EVERY rebase, `grep -n attempt_started_at src/`.
- 🛑 I once committed without staging `tasks.py` (Codex caught HEAD still broken): check
  `git status --short | grep -v '^??'` is empty after each commit.
- Codex prompts that embed a plan must say "PLAN review; code is today's; do not report 'not
  implemented'". Plan reviews took 8 rounds each; diff reviews widen scope every round (scope the
  invariant explicitly in the plan to stop that).
- Python patch scripts: files are CRLF/LF mixed; build anchors with the file's own newline, or
  use the Edit tool. `str.replace` with `$` is fine but prefer slicing for moves.
- `git stash` is shared across worktrees (used once, popped immediately; avoid).

## Next steps (exact)
1. `cd C:/Users/Windows/bl-wt/fence`; `git fetch`; `git rebase origin/main`; resolve; grep for
   `attempt_started_at`; ruff; `tests/test_finalize*.py` green.
2. Fix r6 **P1** (skip-trace enqueue fence) RED-first, then r6 **P2** (terminal vs lost at early
   returns); mutation-check; update the plan's Review.
3. Full suite (8 parts), security review x2, Codex diff r7 (quote r6 findings; say the two
   log-publish P2s are owner-accepted) until GATE: PASS.
4. Open PR, owner scales worker to 0, quiet.py zeros, merge `--match-head-commit`, verify SHA on
   api/worker/beat + `/health` + logs, owner restores worker. No migration in 2c-bis.
5. 2c: re-apply 106 on the test DB, rebase onto main (with 2c-bis), move the snapshot decision
   into finalize.py, full suite, Codex until PASS, same quiesced merge (migration 106: verify the
   six columns via api/worker/beat connections), first eligible real job's snapshot sums.
6. FE follow-up PR (types regen from BE main + render breakdown), then BUILD_JOURNAL entry
   (newest on top; record the failures above honestly).
Queued follow-ups: stale-attempt log-publish races (accepted P2s); export object key per
attempt; widen fence to plan-cap/enrichment writes; `connector_unavailable` refusal;
`jobs.was_ai` snapshot; `/scrapers` label overlap; `test_rls_isolation` role GRANTs.
