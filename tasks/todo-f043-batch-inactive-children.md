# F-043: a deleted batch scraper keeps being scraped and billed

Source: UX audit F-043 (FE repo `docs/ux-audit/UX-AUDIT.md`), Phase 3.0 contract Q3.
Worktree `C:/Users/Windows/bl-wt/f043`, branch `fix/f043-batch-inactive-children` from origin/main `4b963d1c`.

## The defects (confirmed in code)
1. `dispatch_batch_run` (`src/workers/batch_tasks.py:146-151`) loads batch children with
   `batch_id` + `user_id` only. `DELETE /scrapers/{id}` is a soft delete (`active = False`,
   `scrapers.py:468`) that keeps `batch_id`, so a deleted child gets a new Job on every batch
   run: scraped, and billed for its new leads. It is the ONLY `trigger="batch"` Job creator.
2. `child_count` and the detail `children` list (`batches.py:573-600` list, `655-700` detail,
   where `child_count = len(children)`) count the same unfiltered set.
3. (found in the Codex consult) Delete does not clear `paused_reason`, and there is no
   `deleted_at`. A scraper deleted while downgrade-paused is indistinguishable from a paused one,
   and the reconcile at `entitlements.py:531` re-activates paused configs once the plan permits,
   so it comes back to life after an upgrade and is scraped and billed. Not batch-specific.
4. (Codex) Today an entitlement-paused child still reaches `config_run_violation`, which only
   counts ACTIVE configs, so it can pass the gate and be scraped while paused.

## Two meanings of active=False
- Deleted by the user: `active=False`, `paused_reason IS NULL`.
- Paused by a plan downgrade: `active=False`, `paused_reason='entitlement'`.

## Phase 1 plan (4 source files + 1 test file)
- [x] 1. One shared SQL predicate for "a current batch child": `active OR paused_reason =
      'entitlement'` (deleted children excluded). Used by the fan-out and both batch routes so
      they cannot drift. (`src/api/entitlements.py`, next to the pause constants.)
- [x] 2. Fan-out (`batch_tasks.py`): filter in the SQL, before the zero-children branch, so an
      all-deleted batch keeps today's zero-child path instead of "all blocked". An
      entitlement-paused child is ALWAYS blocked and reported as "plan limit", never given a Job,
      whatever `config_run_violation` says.
- [x] 3. Batch routes (`batches.py`): list `child_count` and the detail `children` list use the
      predicate. Deleted children leave the current list; their past Jobs and run history are
      untouched.
- [x] 4. Delete (`scrapers.py`): also set `paused_reason = None`, so a deleted scraper can never
      be read as paused or revived by the reconcile.
- [x] 5. Tests (`tests/test_batch_dispatch.py`), real DB, isolated `_test` DB only:
      mixed active + deleted + paused children (active gets a Job, deleted none, paused reported
      "plan limit" and none); paused child with no competitors still gets no Job; paused-only
      batch fails with "plan limit"; all-deleted batch creates no Job and keeps the zero-child
      status; list and detail `child_count` agree and exclude deleted; deleting a paused scraper
      clears `paused_reason` and the reconcile does not revive it.
- [x] 6. Verify: targeted pytest, ruff, Claude security review (§14: billing, tenant scoping),
      Codex diff review. Any P1 = NO-GO.

## Decided, with reasons
- Deletion is effective for FUTURE dispatches. A Job already created before the delete may still
  run (the running-branch re-enqueue uses the run's recorded `child_job_ids`, not the configs).
  Cancelling queued work is a separate change.
- A downgrade-paused child stays visible and is reported, rather than silently dropped, so the
  user sees why it did not run.

## Out of scope (Phase 2, needs its own approval)
- Zero-child runs recorded as `skipped` + reason instead of `done`; auto-pause a parent with no
  current children; batch pause/delete endpoints; purge-script schedule guard.
- EXISTING prod rows deleted while paused (`active=False`, `paused_reason='entitlement'`, deleted
  by the user) cannot be told apart from genuinely paused ones without the `scraper_deleted`
  audit log. Owner decision: a read-only prod query cross-checked against the audit log, then a
  one-off data fix.

## Risks
- Deploy = merge to main (Railway). No migration in Phase 1. Quiesce before merging.
- pytest has wiped prod twice: only `TEST_DATABASE_URL` on a local `_test` DB, never bare pytest.
- Codex consult 2026-09-26: PLAN: CHANGES, all folded in above (predicate kept, paused child is an
  unconditional block, filter in SQL, delete clears `paused_reason`, the test matrix).

## Review (2026-09-26)
- Red then green: the 5 new tests FAIL on unfixed origin/main (deleted child got the Job, a lone
  paused child was scraped, the all-deleted batch took another path, counts included the deleted
  child, delete left paused_reason set) and pass here. A 6th test covers the Codex P2 below.
- Related suites: 519 passed, 1 skipped (batch, entitlement, scraper, reconcile files); ruff clean.
- Claude security review (billing, tenant): every query keeps its user_id scope; no new input,
  schema or client error; paused children are blocked regardless of the enforcement flag.
- Codex diff review: GATE: FAIL.
  - [P2] paused child blocked only when active=False. FIXED: blocked on the reason alone, tested.
  - [P1] check-then-insert race: a child deleted between the fan-out SELECT and COMMIT still gets
    a Job, and run_scrape_job never re-checks config.active. OPEN. NO-GO for merge.
  - [P3] test gaps (concurrency, history of deleted children, cross-tenant, persisted reconcile).

## Phase 1b (approved 2026-09-26, DONE): execution-time guard
- Rejected: SELECT ... FOR UPDATE on the children. Under RLS, FOR UPDATE returns only rows that
  also pass the UPDATE policy. This repo does not define the worker role's policy on
  scraper_configs, so it could silently drop every child in production (every batch "done" with
  no Jobs) while passing locally, where tests run without RLS.
- Proposed: in run_scrape_job (src/workers/tasks.py), right after the pending to queued claim, a
  job whose config is not active or carries paused_reason='entitlement' is cancelled with a plain
  message and no scrape. That closes the race, and also jobs queued before a delete and every
  other dispatch path (manual, scheduled, watchdog). Nothing legitimately runs inactive configs:
  the preview path that did was removed in #128.
- Tests: a pending Job whose config is deleted after dispatch is cancelled and never scraped; a
  paused config's Job is cancelled; an active config still runs.

## Phase 1b review (2026-09-26)
- Built: skip_reason_for_config() plus a check in run_scrape_job right after the claim. A job
  whose scraper is deleted or plan-paused ends through _fail_job with a plain reason, before any
  scrape. _fail_job releases any reserved quota; no job_failed notification is emitted (those are
  emitted explicitly at other call sites, never by _fail_job).
- Red then green: 4 new tests fail on the Phase 1a commit (the jobs ran on until connector
  lookup, which only a no-connector test county stops) and pass here. The tests use a county with
  no connector, so they can never reach a live county site on any branch.
- Full suite in 4 batches: 4,651 passed, 2 skipped, 0 failed. ruff clean.
- Codex: GATE: PASS, its P1 resolved. Remaining:
  - [P2] a skipped batch child counts as failed, so the run can read "partial". Accepted for now;
    the proper "skipped" status is the Phase 2 status-model change.
  - [P2] misleading failure notification: does not apply, verified in code (see above).
  - [P3] a delete that lands between this check and the scrape (milliseconds) can still run once.
    Inherent to a lock-free check; the queued-job race it was built for is closed.
