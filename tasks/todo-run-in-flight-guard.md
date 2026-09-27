# 2a · F-003 / Phase 3.0 Q5: one active run per scraper

Source: UX audit F-003; `docs/ux-audit/phase-3.0-contracts.md` Q5 (FE repo). Worktree
`C:/Users/Windows/bl-wt/run-guard`, branch `fix/run-in-flight-guard` from origin/main `406cff6e`.

## The defect (re-verified on 406cff6e)
`POST /jobs` (`create_job`, jobs.py:324, the only caller of `enqueue_scrape_job`) checks that the
config exists, is active and is the user's, plus a 5/min rate limit, but never whether a job is
already active for that config. The scheduler has that check (`_scheduled_dispatch_blocker_exists`,
dispatch.py:30); manual runs, a second tab, the dashboard and new-scraper pages, and API-key
clients do not. Two concurrent runs of one config both scrape the county, and the
`delivered_records ON CONFLICT` dedup splits the new leads between them by race, so each run's page
shows part of the set and calls the rest "already delivered". The FE guard is client-only.

## Production facts (read-only, 2026-09-27)
- No config has >1 active job now; jobs is 136 rows / 456 kB (index build is instant).
- 0 overlapping job pairs in the last 60 days: prevention, not a live incident.
- Existing jobs indexes: ix_jobs_scraper_config_id, ix_jobs_status, ix_jobs_user_created,
  ix_jobs_user_id, jobs_pkey, uq_jobs_id_user, uq_jobs_scheduled_occurrence.

## Compatibility (checked in code)
- Watchdog re-queue UPDATEs the same row back to 'pending' (health.py:307), never inserts: safe.
- Scheduler inserts with `pg_insert(...).on_conflict_do_nothing()` and NO conflict target
  (dispatch.py:185), so a clash with the new index is silently skipped, which matches its intent.
- Batch fan-out inserts child Jobs through the ORM (batch_tasks.py:~204) with no conflict handling:
  one child already active would abort the whole fan-out. Needs per-child handling.

## Plan (BE: 4 files)
- [ ] 1. Migration 104: partial unique index `uq_jobs_one_active_per_config` on
      `jobs(scraper_config_id) WHERE status IN ('pending','queued','probing','scraping','enriching')`,
      built CONCURRENTLY in an autocommit block with migration 100's invalid-index handling.
      Downgrade drops it.
- [ ] 2. `enqueue_scrape_job`: before inserting, look up an active job for the config (user-scoped)
      and raise 409 `{"code":"run_in_flight","job_id":"...","message":"This scraper is already
      running."}`. An IntegrityError on the new index at flush (a race) rolls back and returns the
      same 409 with the winner's job id.
- [ ] 3. Batch fan-out: each child Job insert in a SAVEPOINT; a clash with this index skips that
      child and reports it in `failed_children` with reason "already running". The rest of the
      fan-out proceeds.
- [ ] 4. Tests (real DB, isolated `_test` DB): second POST /jobs -> 409 with the first job's id;
      allowed again once the first is terminal; a direct duplicate insert hits the index and maps to
      409; a batch child with an active job is reported and its siblings run; a scheduler duplicate
      insert is a no-op; the watchdog re-queue of the same row still works; a drift guard that the
      index predicate equals ACTIVE_STATUSES.
- [ ] 5. Verify: targeted + full pytest in batches, ruff, security review (§14), Codex diff review.
- [ ] 6. FE follow-up (separate small PR): on 409 `run_in_flight`, go to `/live/{job_id}` instead of
      showing an error (Scrapers, Dashboard, New Scraper "Run now").

## Codex consult (2026-09-27): PLAN: CHANGES, all adopted
- Index confirmed as the right primitive (covers API, workers, scheduler, scripts; RLS does not
  weaken uniqueness). The API pre-check is UX only; the index is authoritative.
- Map ONLY SQLSTATE 23505 on `uq_jobs_one_active_per_config` to 409; roll back first, then look up
  the winner in a new transaction (RLS GUC re-applies on the next transaction). `job_id` may be
  null: the winner can finish between the conflict and the lookup.
- [P1] Cancel: a cancelled worker that is still mid-scrape can write dedup claims before it notices,
  and a new run started meanwhile freezes those leads as "already delivered" (memory landmine
  "releasing a claim does not unsay it"; `_release_claims_of_cancelled_job` releases later but
  cannot repair the other run). Liveness cannot be read after a cancel: the heartbeat SQL skips
  cancelled rows (status.py:822), so `last_heartbeat_at` freezes at the cancel. The API therefore
  applies a cooldown: a job a worker had STARTED (`started_at` set) and that was cancelled within
  the last 5 minutes (`finished_at`) -> 409 `run_in_flight`, "This scraper is still stopping.
  Try again in a few minutes." A job cancelled before any worker claimed it never blocks.
  Documented limit: a worker slower than 5 minutes to observe a cancel could still overlap. The
  index cannot express this; the API check covers the path users take right after cancelling.
- Batch fan-out: flush run state before the per-child SAVEPOINTs; catch only this index's
  violation; roll back the savepoint and expunge the failed child; siblings and `failed_children`
  still commit; include the existing `job_id` in the reason; all-conflicting keeps the existing
  terminal behavior.
- Scheduler: an active-run conflict on insert is logged as such, not as "occurrence already
  dispatched".
- Migration: preflight for duplicate active rows (clear error, no half-built index), invalid-index
  recovery as in 100, downgrade documented as removing the invariant.
- Tests added: two truly concurrent POST /jobs (asyncio.gather) -> exactly one 201 and one 409;
  every active status blocks and every terminal status allows; a cancelled job with a live
  heartbeat blocks, a stale one allows; unrelated IntegrityErrors are not mapped; batch with one,
  several and all children conflicting; scheduler conflict is a no-op; watchdog re-queue of the
  same row; the next request after a 409 works (session/RLS recovered); tenant scoping of the
  winner lookup (never another user's job id).

## Risks
- Deploy = merge to main; the migration runs on boot (migrate.py, advisory-locked). Quiesce check
  before merging. The index build is instant at this size.
- If a duplicate active pair appears between now and the deploy, CREATE UNIQUE INDEX fails and
  leaves an INVALID index; migration 100's pattern handles re-runs, and a pre-merge prod check
  guards it.

## Build and review log (2026-09-27)
- Built: migration 104 (verified: valid unique index, predicate read back from Postgres, downgrade /
  upgrade / re-run idempotent; its duplicate preflight fired for real on a dirty test DB);
  `enqueue_scrape_job` pre-check + 409 `run_in_flight`; race path maps ONLY this index's 23505;
  batch fan-out per-child SAVEPOINT ("already running") and cooldown check ("still stopping");
  scheduler logs an active-run skip as such. One shared rule, `Job.holds_run_slot(now)`, used by
  POST /jobs, the scheduler blocker and the batch fan-out.
- Bug found by the suite, fixed: on the race path the rollback expired `config.id`, and reading it
  lazy-loaded outside the async context (500 instead of 409). Ids are captured before the flush;
  a deterministic race test (an uncommitted competing insert held open) now forces that path.
- Tests: 20 new (11 fail on unfixed main). Existing tests that built two ACTIVE jobs on ONE
  scraper now give each concurrent job its own scraper (claim transfer, quota reservation,
  skip-trace claim, dispatch null-occurrence): the rules they test are account-wide.
- Full suite 4,845 passed / 0 failed before the cooldown-everywhere change; after it, every
  scheduling/dispatch/batch/job/watchdog/quota test file: 547 passed / 0 failed. ruff clean.
- Codex diff review 1: GATE: FAIL.
  - [P1] cooldown only on POST /jobs: FIXED, `Job.holds_run_slot` on all three start paths, tested.
  - [P1] a manual run can make the scheduler dispatch the skipped occurrence later: NOT changed.
    Pre-existing: `_scheduled_dispatch_blocker_exists` already skips while a job is active and a
    later tick can dispatch once it ends; the index only makes that check race-safe.
  - [P2] rollback drops other request writes: none exist before the insert (reads only).
  - [P2] finished_at on cancel: set by the cancel route and by batch force-finalize.
  - [P2] claim tests lost same-scraper coverage: claims are keyed (user_id, dedup_hash),
    account-wide, so two scrapers is the realistic case.
  - [P2] RLS after rollback: the after_begin listener re-applies the GUC (deps.get_rls_db docs).
  - [P2] migration preflight is not race-free: a duplicate slipping in leaves an INVALID index;
    104 drops and rebuilds it on the next run (100's pattern). Quiesce check before merge.
  - [P3] scheduler log can mislabel: logging only.
- Codex re-review: cooldown-everywhere P1 confirmed resolved. It raised two P1s, both DOWNGRADED BY
  CODEX to documented P3 residuals after the evidence: (a) the scheduler can only catch up a
  skipped occurrence within its +/-1 beat-tick window (dispatch.py:18), never hours late;
  (b) the cooldown is not DB-enforced: slipping past it needs another run to be created, claimed
  by a worker and cancelled inside one request's pre-check-to-insert gap (milliseconds vs a
  multi-second claim). Future hardening: a slot table or an exclusion constraint. P3 stale batch
  comment fixed. Final: GATE: PASS.
