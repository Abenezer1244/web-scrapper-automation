# UX 2d (Q2, F-006): "already delivered" on the jobs list and the completion notification

Branch `feat/jobs-already-delivered-2d` (worktree `bl-wt/delivered2d`, from origin/main `d1003700`).
Contract: FE `docs/ux-audit/phase-3.0-contracts.md` "Q2". Audit finding F-006 (P1).

## Problem
Outside a run's own page, a run shows only its new, billed `record_count`. A King probate run
that found 125 leads, all delivered before, reads "0" in the Results index and "0 new records"
in the notification: the same "0" as a county that returned nothing.

## Answer to Q2
**Yes, since 2c, when a valid snapshot exists.** `GET /jobs` returns `JobResponse.breakdown`
(snapshot frozen in the done-CAS, validated by `breakdown_from_job`; a rejected snapshot shows
nothing and is logged). A job finished after 2026-09-30 11:31Z has one unless `snapshot_columns`
refused it: retried run, `records_found` missing, unclassified rows, more saved than found, or
the attempt did not own a fresh bill. `breakdown.already_delivered` is the
number F-006 needs. No API change is required for the list.

## Why no live fallback on GET /jobs (scope: the LIST only) (measured, prod, read-only, 2026-09-30)
The shell (`DashboardShellClient`) polls `GET /jobs` every 5 s on every dashboard page; the
Results index calls `GET /jobs?exclude_batch_children=true` once per visit.
- Full live partition over the heaviest account's newest 100 jobs (103k rows): 3.9-5.7 s.
- Only the `already_delivered` bucket (owner-joined, `is_duplicate IS TRUE AND
  already_delivered_sql AND address_actionable_sql`, 30 sequential samples per account):
  heaviest (87 done jobs) first 10.9 s, p50 0.70 s, p95 9.9 s, p99 12.1 s; second account
  first 4.5 s, p50 45 ms, p95 2.1 s; small accounts ~13 ms. (No concurrent load test: this is
  the shared prod DB.)
Even opt-in (Results index only) a p95 of ~10 s on one page load is not acceptable, and a
statement timeout would silently hide the number from exactly the account with the most
history. Codex r1 P1s on mixing snapshot and live semantics and on rejected snapshots becoming
live candidates are resolved by not having a live path on the list. (Codex r1 raised the cost as P2.)
The run page (`GET /jobs/{id}/results`) keeps its 2c live fallback for ONE job (bounded by that
job's rows, not 100 jobs'); unchanged here and out of scope (Codex r2 P2).

## Decision (recommended)
1. **List: snapshot only, no API change.** FE reads `breakdown?.already_delivered` from the
   existing field. Runs without a snapshot (every run before 2026-09-30 11:31Z, and retried
   runs) show the new count alone, as today: no number is invented. The run page still shows
   their live breakdown on click.
2. **Notification: add the persisted snapshot's count.** The `job_completed` detail gains
   `already_delivered` read from the JOB ROW via `breakdown_from_job(job)` (job is refreshed at
   the end of `finalize_billing_and_done`), NOT from `_outcome.frozen`: the notification then
   carries exactly what `GET /jobs` shows, and a billed re-run that kept an earlier snapshot
   still reports it (Codex r2 P1; that path is practically unreachable since billing and done
   commit together, but the row read closes it for free). 0 included (a frozen zero is a real
   zero). Key ABSENT when there is no valid snapshot (never 0 for "unknown"). Old notifications
   are not backfilled. `notifications.detail` is JSON (`models.py`, migration 065), typed
   `dict | None` in `NotificationResponse`: no migration, no API schema change.
3. **Scope wording** (FE and API docs): "already delivered" is ACCOUNT-WIDE: dedup_hash has
   no county, record type or scraper, so a lead counts if ANY earlier run of this account
   delivered it. It is the RAW breakdown bucket (no tax cap), the same number as the run
   page's headline, not the tax-capped results tab. `JobResponse.breakdown` becomes
   `Field(default=None, description=...)` stating this (today only a code comment), and the
   `ResultsPage.breakdown` description says the same (docs-only schema change -> openapi regen).
4. **History (owner choice, NOT in this plan by default):** option B = migration 107 with a
   nullable `jobs.already_delivered_counted` + `..._counted_at`, filled ONCE by an ops script
   for done jobs with no snapshot (throttled, one job per statement), shown by the FE as
   "N already delivered (counted <date>)". Costs a migration + script + backfill run; only
   needed if the owner wants old runs fixed in the Results index.

## Phases
### 2d-i: BE (one PR, two phases of <= 5 files, owner checkpoint between; merged once)
#### Phase A: completion notification (5 files)
- [ ] `src/workers/tasks_helpers/finalize.py` (beside the other completion code; module
      docstring updated, it says emission stays in tasks.py today, Codex r3 P3):
      `emit_job_completed(job, config, display_count)` builds the detail (scraper_name, county,
      record_count, and `already_delivered` from `breakdown_from_job(job)` when valid) and calls
      `create_notification`. `src/workers/tasks.py` replaces its inline block with this one
      call, so the tested function IS the production emission (no copied logic). Add
      `emit_job_completed` to the `tasks.py` import list from `tasks_helpers.finalize`
      (`src/workers/tasks.py:70-77`), else NameError at the call (Codex r4 P1).
- [ ] `docs/superpowers/specs/2026-06-18-notifications-design.md` (:36 detail contract and
      :77 call site): the `job_completed` detail gains optional `already_delivered` (present
      incl. 0 = valid snapshot; absent = unknown) and the call site is `emit_job_completed` in
      `tasks_helpers/finalize.py` (r4 P3, r6 P3).
- [ ] `tests/test_completion_notification.py` (real test DB, real `notifications` rows, prefs
      enabled; each proven RED on unfixed code):
  - done job with a valid snapshot, already_delivered 123 -> row detail has 123;
  - valid snapshot with already_delivered 0 -> detail has 0 (present, not absent);
  - no snapshot (all six NULL, e.g. a retried run) -> key absent; record_count, scraper_name,
    county unchanged from today;
  - rejected (partial / non-reconciling) snapshot -> key absent;
  - end-to-end through `finalize_billing_and_done` then `emit_job_completed` for a fresh
    non-retried run (freezes -> key present = frozen value) and a retried one (key absent);
  - pref `job_completed = False` -> no row (unchanged behaviour);
  - billed re-run (Codex r3 P2): a job with a valid snapshot (already_delivered 123),
    `billing_applied_at` set, rows changed since; `finalize_billing_and_done` returns
    `frozen=None` and keeps the columns; `emit_job_completed` still writes 123;
  - source guard: `run_scrape_job` calls `emit_job_completed(` exactly once and no longer
    calls `create_notification(` with `type="job_completed"` itself.
- [ ] `tests/test_finalize_fence.py:545-550`: the existing post-DONE-tail guard requires
      `create_notification`; change it to require exactly one `emit_job_completed(` after the
      DONE check and forbid an inline `job_completed` emission (Codex r3 P1).
#### Phase B: API docs + list test (2 files + generated openapi.json)
- [ ] `src/api/schemas.py`: `JobResponse.breakdown` + `ResultsPage.breakdown` descriptions
      (account-wide, raw, snapshot vs live) via `Field(description=...)` (docs only).
- [ ] `schema/openapi.json` regenerated (`export_openapi.py`, then `--check`).
- [ ] `tests/test_run_breakdown_api.py`: add the explicit `GET /jobs` list case for a
      no-snapshot job: `breakdown` and `breakdown_basis` null, no results-partition query
      (statement counter) (Codex r2 P2: today only `GET /jobs/{id}` is asserted); and a
      REJECTED (partial) snapshot on `GET /jobs`: null breakdown, zero partition queries (r4 P2).
- [ ] Full suite 8 parts; security review x2; Codex diff review until GATE: PASS; quiet.py;
      merge; verify prod (worker deploy SUCCESS; read-only): select the first job FINISHED
      AFTER the worker deploy with a valid snapshot; THEN require its `job_completed`
      notification (created after the deploy) and assert detail `already_delivered` equals the
      row's `breakdown_already_delivered`. If that user's `job_completed` pref is off, say so and
      take the next job. A job qualifies only if it was CREATED and STARTED after the worker
      rollout completed (an older job may have run on the old worker; r7 P2). A missing
      notification for a qualifying job is a FAILURE (after a
      bounded wait of 10 min past finished_at), not pending. PENDING only while no qualifying
      job exists. A post-deploy job without a snapshot must have no key (r4-r6 P2).
### 2d-FE (bridgeleads-web, after 2d-i is live; no types regen needed unless descriptions)
- [ ] Update FE `docs/ux-audit/phase-3.0-contracts.md` Q2 + its summary: list is snapshot
      only (measured cost), live fallback only on the single-run results page (r7 P2).
- [ ] Results index row: `"0 new · 123 already delivered"` when `breakdown` is present and
      `already_delivered > 0`; the count alone otherwise. Dashboard scrapers table: same.
      Bell: same from `detail.already_delivered` when present. Old detail shape (no key) keeps
      today's text. Accessible text: title/sr-only "Delivered to you by an earlier run of any
      of your scrapers". No em dashes. Stub API + Playwright proof at 1440 and 390.

## Reconciled disagreement with Codex
- r7 P1 claimed the FE run page headline still shows the live, tax-capped
  `already_delivered_count`. Checked FE master (`app/(dashboard)/results/[id]/page.tsx`,
  FE #169): the headline `new` uses `breakdown.new` when the basis is snapshot, the live
  "N already delivered" link renders only when `!resultsPage?.breakdown`, and the breakdown
  section shows `breakdown.already_delivered`. So list, notification and run page already agree
  whenever a snapshot exists; the tax-capped count stays on the tab, as 2c decided. Not adopted.

## Risks
- History stays count-only until those runs age out of the window, unless the owner picks B.
- A retried run never gets a snapshot, so it never shows the split (by 2c design).

## Open questions for owner
1. Accept snapshot-only for the list (recommended), or add option B (history backfill, mig 107)?
2. Show the source scraper when the earlier delivery came from a DIFFERENT scraper (contract Q2
   open question)? Recommend NO for the list (one number); the run page lists sources.
