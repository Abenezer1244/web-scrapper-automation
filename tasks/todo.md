# Pierce Probate job stuck after retry (job 9c8b7259) — investigation + fix

Branch: `fix/pierce-retry-stuck` · worktree `C:/Users/Windows/bridgeleads-worktrees/pierce-retry-stuck`

## Evidence (production, read-only)

**The job**

| field | value |
|---|---|
| job_id | `9c8b7259-a940-454e-b510-a074b861af59` |
| scraper_config_id | `ea533c9e-d194-4f74-a8e7-4b32e9d428e1` ("Tester", pierce/WA/probate) |
| user_id | `e73585c6-e10d-48e3-941f-28090380ff51` (starter plan) |
| trigger | manual |
| status | `scraping` (still, at 09:38 UTC = 23m52s) |
| retry_count | 1 |
| created_at | 2026-09-09 09:07:51.596 UTC |
| started_at | 2026-09-09 09:14:10.489 UTC (attempt 2's claim) |
| finished_at / error_message | NULL / NULL |
| last_heartbeat_at | **NULL** |
| record_count / page_current / page_total | 0 / 0 / 0 |
| reserved_count / reserved_at | 0 / NULL |
| billed_count / billing_applied_at | 0 / NULL |
| attempt 1 celery task | `05bca6e1-35f3-467f-9e0c-2ee42b5ff870` |
| attempt 2 celery task | `0172959c-f3ad-4e4d-8af6-d10cd1a2faad` |

**Attempt 1 — the real exception (worker deployment `c5c12eca`)**

```
09:07:52.143 run_scrape_job[05bca6e1] received
09:07:54.334 Browser context started (headless=False, DISPLAY=:99, chromium=151.0.7922.34)
09:07:54.853 Navigated to https://armsweb.co.pierce.wa.us/
09:07:54.924 Disclaimer accepted
09:07:55.173 Navigated to .../RealEstate/SearchEntry.aspx
09:08:11.31  playwright._impl._errors.TimeoutError: Locator.wait_for: Timeout 15000ms exceeded.
             Call log: waiting for locator("input[title*=\"Date Filed From\"]").first to be visible
             at src/scrapers/pierce_wa_probate.py:305 in _fill_search_form
09:08:11.588 transient scrape error - re-queued (retry 1/2, countdown 358s)
09:08:11.590 run_scrape_job[05bca6e1] succeeded in 19.44s  <- attempt 1 EXITED here
09:08:11.560 run_scrape_job[0172959c] received  <- retry prefetched + held for its ETA
```

Upstream render flake on the ARMS ASP.NET search form. Correctly classified transient,
correctly retried. Not a BridgeLeads defect.

**Attempt 2 — it did NOT stall at "Connecting to county portal"**

```
09:13:30 UTC  worker deployment c9f9f3df goes SUCCESS  <- ROLLOUT STARTS
09:14:07-15   new replicas boot and go "ready"
09:14:10.489  job claimed (started_at) -- by ForkPoolWorker-12 on the OLD container
09:14:13.200  Browser context started
09:14:14.309  Disclaimer accepted
09:14:14.735  Navigated to SearchEntry.aspx
09:14:17.955  Typed date range: 08/10/2026 - 09/02/2026   <- form filled fine, flake cleared
09:14:18.225  Checked 1 doc types for PROBATE
09:14:23.663  Search: 38 records found
09:14:23.666  Total pages: 2
09:14:23.667  Processing page 1
              worker: Warm shutdown (MainProcess) / Stopping Container
              *** LOG ENDS. Container killed ~13s into the scrape phase. ***
```

## Root cause

1. The retry task was prefetched and held in memory by a worker on the OLD deployment
   for its 358s ETA. That ETA came due ON SCHEDULE at ~09:14:09 (09:08:11 + 358s)
   while the old container was still alive and serving; the rollout did not cause it
   to fire early. The container was then stopped at ~09:14:23, ~13s into the scrape
   phase, and killed the attempt in flight. (Codex corrected my first, stronger
   causal claim here; the arithmetic confirms Codex.)
2. A SIGTERM/stop leaves no Python-level exception, so `_RunScrapeJobTask.on_failure`
   never runs. The row is stranded at `status='scraping'`.
3. `acks_late` redelivery is a deliberate no-op here: the claim CAS only accepts
   `pending`, and the row is `scraping`. Recovery is owned solely by the watchdog.
4. **`HeartbeatThread` has been disabled since the 2026-06-18 pool-deadlock rollback**
   (`tasks.py`: `# _hb.start(job.started_at)  # DISABLED`). So `last_heartbeat_at` is
   permanently NULL on every job in production (verified: all 11 pierce/probate rows
   have `hb=None`). The watchdog's fast 15-minute stale-heartbeat branch is DEAD CODE.
   Every worker-lost job falls to the NULL-heartbeat fallback `started_at < now-70min`.
   This job would have shown "Scraping records... LIVE" until **10:24:10 UTC**.
5. The UI has no liveness input at all: `JobProgress.model_post_init` maps a non-terminal
   status straight to an in-progress label, and the stream badge says LIVE because the
   job is non-terminal. There is no "waiting to retry" state either.

## Secondary defect found

`src/workers/tasks.py:603` assigns `job.progress_label` and commits inside a try/except
that logs "Failed to commit progress_label". **`progress_label` is not a column on `Job`**
and is in no migration (prod confirms `column j.progress_label does not exist`); it is a
computed Pydantic field on `JobProgress`. The assignment is a no-op on a plain Python
attribute, the commit writes nothing, and the handler can never fire. Dead code that reads
as a working feature.

## Verified NOT broken (do not "fix")

- **No duplicate execution.** Attempt 1 returned at 09:08:11.590 before attempt 2 ran at
  09:14:10. The `pending`->`queued` CAS makes concurrent execution structurally impossible.
- **No quota/billing duplication.** reserved=0/NULL, billed=0/NULL. The plan cap and the
  reservation run *after* enrichment, so a scrape-phase retry cannot double-reserve;
  `_retry_scrape_job` additionally refuses when `billing_applied_at IS NOT NULL`.
- **Pierce ARMS is healthy.** Attempt 2 reached the results grid: 38 records, 2 pages.
- **Transient classification is correct.** Playwright `TimeoutError` -> retryable.

## Plan

- [x] 1. Consult Codex on the fix design; reconcile before coding. (see Codex section)
- [x] 2. `src/db/session.py`: dedicated NullPool heartbeat engine + `heartbeat_sync_session()`,
      isolated from the pool_size=2 work pool (the documented precondition for re-enabling).
- [x] 3. `src/workers/tasks_helpers/status.py`: point `_write_heartbeat` at that session.
- [x] 4. `src/workers/tasks.py`: re-enable `_hb.start(job.started_at)`; delete the dead
      `progress_label` write; make the transient retry log carry phase + exception type,
      and the user-facing line carry the attempt number without internal detail.
- [x] 5. `src/api/schemas.py`: honest job presentation - "waiting to retry" for
      `pending`+`retry_count>0`, and a stalled signal when an active job has no live worker.
- [x] 6. Tests for each of the above.
- [x] 7. Verification: `ruff` clean; full `pytest` green on an isolated test DB.
- [x] 8. Codex diff review; reconcile. (see "Codex diff review" below)

## Codex consult, reconciled

Codex's verdict was "block F3 until stale attempts are fenced from writing", on the
grounds that faster recovery could repeat the 2026-06-17 result-duplication incident
and that reservation/billing safety was unproven. It said so explicitly WITHOUT repo
access, flagging unverified risk rather than an observed defect.

**Independently verified in the code, and the blocker does not stand.** Three
per-JOB (not per-attempt) idempotency gates already fence a superseded attempt:

| side effect | gate | where |
|---|---|---|
| result rows | `ON CONFLICT (job_id, source_fingerprint) DO NOTHING` | migration 062, `tasks.py:914` |
| quota reservation | CAS `UPDATE jobs SET reserved_at=... WHERE reserved_at IS NULL` | `tasks.py:1519` |
| billing | CAS `UPDATE jobs ... WHERE billing_applied_at IS NULL` | `tasks.py:1922` |

Per-job CAS is *stronger* than per-attempt fencing for this purpose: the outcome is
idempotent no matter which attempt wins the race. Migration 062 exists precisely
because of the 2026-06-17 incident Codex was worried about.

**Codex findings ACCEPTED (each verified in code first):**

1. **Stale-heartbeat inheritance = a real retry storm, activated by F3.** Verified:
   `_watchdog_stuck_jobs_impl` resets status/started_at/counters but NOT
   `last_heartbeat_at`, and the claim CAS never stamped it. Dormant only because the
   column is always NULL today. Fixed on both layers: the claim stamps a fresh
   `last_heartbeat_at`, and the watchdog re-queue nulls the dead attempt's.
2. **`lock_timeout` below `statement_timeout`** on the heartbeat engine. The heartbeat
   UPDATE takes the `jobs` row lock that the long-lived work session also holds.
3. **F4: "the except can never fire" was wrong of me.** `db.commit()` can fail for
   unrelated reasons. The commit is also load-bearing (it flushes `date_from`/`date_to`
   before a call that can run 30 minutes), so only the dead assignment was removed and
   the log message corrected.
4. **F5: do not promise a time when the broker publish failed.** Verified: the old code
   emitted "retrying in ~5 min" even on a publish exception. Now branch on it.
5. **F6 wording:** NULL heartbeat means UNOBSERVED, not dead. The copy avoids claiming
   a dead worker, and a NULL heartbeat falls back to the watchdog's own 70-minute
   cutoff rather than being called stalled early.
6. **Causal correction on the ETA** (folded into Root cause above).

**Codex points NOT actioned, with reasons:**

- *"Verify port 6543 is really pgbouncer, not Supavisor"* — true but pre-existing and
  identical for the work engine; the heartbeat engine reuses the same proven DSN.
- *"Replace ETA retries with an outbox / durable dispatch"* — Codex itself scoped this
  out. The stranded-retry watchdog branch already recovers a failed publish.
- *"5 minutes is not evidence-based"* — agreed it is unproven, so the backoff is
  UNCHANGED. Changing it was never in scope.
- *"A heartbeat can keep beating while the scrape is wedged"* — true, and bounded
  already: `_SCRAPE_TIMEOUT` 1800s, soft limit 3600s, hard limit 3900s.

## Codex diff review (post-implementation gate)

Ran against the `src/` diff with no repo, shell or git access (inline diff only, so Codex
could not mutate this worktree). Verdict **GATE: PASS, no P1**. Codex explicitly caveated
items 1, 2, 3 and 6 as "unverifiable from this diff" because it saw only the changed
hunks; each was verified here against the full files:

| Codex question | Independent verification in this tree | Outcome |
|---|---|---|
| 1. Can attempt 1's heartbeat mask a dead attempt 2? | `_HEARTBEAT_SQL` pins `started_at` AND excludes terminal statuses, so a superseded attempt updates 0 rows and self-reaps within one interval | No defect |
| 2. Can attempt 2 block on attempt 1's lock? | The only `FOR UPDATE` in the work path is `FOR UPDATE OF u` on `users`, never `jobs`; the work session commits before the long scrape, so no `jobs` row lock is held across it | No defect |
| 3. Double reserve / double bill? | Reservation `WHERE reserved_at IS NULL` and billing `WHERE billing_applied_at IS NULL` are per-JOB CAS, and this diff touches neither path | No defect |
| 6. Cancellation vs heartbeat | The heartbeat CAS excludes `done/failed/cancelled`, so it cannot stamp or resurrect a cancelled job | No defect |

**Codex findings ACCEPTED and fixed in this branch:**

1. **[P3] Threshold sharing was incomplete.** `ZOMBIE_UNSTARTED_MINUTES` was introduced and
   consumed by the API, but `health.py` still computed `queued_cutoff` from a bare
   `timedelta(minutes=10)` - precisely the drift the shared-constants block exists to
   prevent, and the constant's own comment already claimed the watchdog read it. Fixed; the
   comment now says what is true.
   Guarded by `test_the_watchdog_reads_the_shared_constants_not_its_own_literals`, which
   reads the watchdog's source and rejects a bare numeric cutoff. Asserting the constants
   equal 15/70/10 (the test that already existed) would NOT have caught this: a literal that
   agrees today still drifts tomorrow. The guard was negative-proved against the pre-fix
   source before being kept.
2. **[P3] "Could not reach the county portal" overstates the diagnosis.** In this very
   incident the portal WAS reached (disclaimer accepted, `SearchEntry.aspx` navigated); it
   was the search form that failed to render, and the transient class also covers resets and
   5xx. Reworded to "The county portal request could not be completed.", which is true for a
   request that fails at any point.

**Codex findings ACCEPTED as bounded, NOT actioned (with reasons):**

- **[P2] No end-to-end network deadline on the heartbeat connection.** True: `connect_timeout`
  plus server-side `statement_timeout`/`lock_timeout` do not bound a black-holed socket
  mid-query. Materially different from the pre-rollback design, though: the heartbeat is a
  DAEMON thread on its own NullPool connection, so a hung ping cannot wedge the scrape or
  block process exit. Worst case it stops beating, the row goes stale, and the watchdog
  re-queues - the designed failure mode, not a new one.
- **[P2] The heartbeat still contends for the `jobs` row lock.** True in principle, bounded in
  practice: the work session commits before entering the scraper, so no `jobs` lock is held
  across the long phase; going stale would need 15 consecutive 60s ticks to lose the lock
  race. `_FAIL_WARN_AT` already logs sustained heartbeat write failure.

## Review

**Recovered, not restarted.** Everything from the pre-restart session survived as uncommitted
work (no commits had been made): the production evidence above, the root cause, the reconciled
Codex design consult, and the implementation of plan items 2-6 across 6 source files plus 2
test files. Nothing was rebuilt.

**What the fix actually changes.** The incident needed two things to go wrong: Pierce flaked
(a genuine upstream render flake, correctly classified and correctly retried), and then a
deploy killed the retry attempt 13s into its scrape. Only the second half is a BridgeLeads
defect, and the defect was not the kill - it was that nothing could SEE the kill.
`HeartbeatThread` had been disabled since the 2026-06-18 pool-deadlock rollback, so
`last_heartbeat_at` was NULL on every job in production and the watchdog's 15-minute
stale-heartbeat branch was dead code. Recovery fell to a 70-minute age fallback, and the UI
had no liveness input at all. This branch re-enables the heartbeat on the isolated NullPool
engine the rollback note demanded, makes the claim stamp liveness so no attempt inherits a
dead one's, and gives the API the same thresholds the watchdog uses.

**Verification.** `ruff` clean. Full suite green on an isolated database
(`bridgeleads_pierce_test`, Redis db 15) so no other agent's rig was touched:
**2777 passed, 2 skipped, 65 deselected, 0 failed** before the Codex fixes, re-run green
after them.

**Known gap, deliberately not closed here (frontend repo, different branch).**
`app/(dashboard)/live/[id]/page.tsx` in `bridgeleads-web` derives its own status text and does
not read `progress_label`, `progress_stalled` or `retry_pending`. So the backend now reports
the truth but the "Scraping records..." label is still client-side. The user-visible win that
lands with THIS branch is the log stream (the retry line is honest now) and the recovery
window shrinking from ~70 min to ~15 min. Wiring the three fields into the live page is a
separate FE change; `bridgeleads-web` is currently checked out on another agent's branch
(`feat/schedule-day-picker`), so it was left alone.
