# Live Run progress: truthful state, not a frozen 0%

BE worktree: `C:/Users/Windows/bl-wt-liverun` — branch `feat/live-run-progress` off `origin/main` @ 6b03ece
FE worktree: `C:/Users/Windows/bl-wt-liverun-fe` — branch `feat/live-run-progress` off `origin/master` @ 6c435d0

> The Desktop checkout was 141 commits behind `origin/main` and the FE checkout was on
> a divergent branch 419 lines behind `origin/master`. Everything below is against the
> upstream code that is actually deployed.

---

## The run in the screenshot was healthy, not stuck (verified in prod)

Job `b80bd9a5-c5f7-4239-9519-71eb8fbc4fa3`, King WA probate, manual, `status=done`.

```
21:54:37  started
21:54:39  "Connecting to county portal..."
          <-- 401s: no log, no status change, page_current=0, page_total=0, record_count=0
22:01:20  "Scrape complete: 57 records found"
22:01:21  Saving records to database...
22:01:22  Checking for duplicate leads...   57 saved (2 new, 55 duplicates)
22:01:23  Building CSV export...  /  Export uploaded to cloud storage
22:01:24  Looking up property and mailing addresses...
22:01:29  "Looking up county records for 48 properties..."
          <-- another 97s of nothing
22:03:07  Found 48/48 mailing addresses
22:03:19  Job complete: 2 new leads (55 duplicates filtered)
```

Heartbeat was alive at 22:02:40. 8m18s of an 8m42s run had nothing measurable.
Final counters: `page_current/page_total = 1/1`, `record_count = 2` (not 57).

---

## Findings

### Why the screen says 0%

| # | Finding | Evidence |
|---|---------|----------|
| **F1** | The ring is fed `progress ?? (isRunning ? 5 : 0)` but the NUMBER inside it is fed `progress ?? 0`. With `progress === null` the arc draws 5% and the label reads **0%**. That is literally the screenshot. | FE `live/[id]/page.tsx:604` vs `:613` |
| **F2** | UNKNOWN is not representable. `jobs.page_current / page_total / record_count` are `Integer NOT NULL DEFAULT 0`. "Not measured yet" and "measured, found nothing" are the same value. | `src/db/models.py:682-684` |
| **F3** | Two fabricated percentages already ship: enrichment renders a hardcoded **90%**, real page progress is capped at **85%**. | FE `page.tsx:307-311` |
| **F4** | The tile is labelled **ETA** but renders `"1m 43s elapsed"`. | FE `page.tsx:337-339, 792` |

### Why there is genuinely nothing to report for minutes

| # | Finding | Evidence |
|---|---------|----------|
| **F5** | **The dead zone.** `"Connecting to county portal..."` and the next log line is `"Scrape complete"`, with the whole scrape in between under a 30-minute timeout. No status change, no log, no counter. | `tasks.py:644` then `:793` |
| **F6** | The 13 `on_progress` call sites are mutually inconsistent: `(0,0,n)`, `(page_num,0,n)`, `(page_num,0,0)`, end-only `(1,1,n)`, `(chunk_num,total_chunks,n)`. Snohomish / trustee_sale / Clark / Whatcom can never produce a denominator. | `src/scrapers/*.py` |
| **F7** | King probate's first `on_progress` fires only after chunk 1 completes, after browser launch, up to 3 startup attempts, a captcha interceptor and a disclaimer accept. `total_chunks` is known before the loop and never reported. | `king_wa_probate.py:165, 228` |
| **F8** | `enriching` is a second mega-stage covering save, dedup, export, address lookup, upload and delivery. It is **not a linear pipeline**: the CSV export runs BEFORE enrichment. | `tasks.py:837-1404`, prod log above |

### What already works (do not rebuild)

- Worker heartbeat is live (`tasks.py:549`), attempt-scoped, 60s.
- `JobResponse.progress_stalled` from `last_heartbeat_at`, on the same thresholds the watchdog recovers on. `schemas.py:1160-1196`
- `retry_pending` (`pending` + `retry_count > 0`). `schemas.py:1197`
- FE already renders stalled runs and gates ETA/LIVE on `isLive`. `page.tsx:301-303`
- FE SSE hook already models `connecting | live | paused | failed | ended` with backoff and lease-aware 429 handling, and already parses a `{"type":"progress"}` event **the backend never emits**. `hooks/use-log-stream.ts:28, 172`

---

## Codex review of the first plan: P1s, all verified by me

Codex (`gpt-6-astra`, high) rejected the first plan's data model and found a live bug.
Every claim below I re-read in the tree myself; all confirmed.

| ID | Sev | Finding | My verification |
|----|-----|---------|-----------------|
| **C1** | P1 | One `last_progress_at` sentinel cannot express independently-known counters. After `(page_num, 0, n)` records and pages-done are known but the total is not. And `records_found` **cannot alias `record_count`**: `done` overwrites it with the billed count. | `tasks.py:2205 record_count=display_count`. Prod: 57 scraped, stored 2. Confirmed. |
| **C2** | P1 | A stage helper calling `commit()` on the work session commits **all pending work**, which can separate billing from the terminal CAS. An independent connection instead blocks on the job-row lock. | `_set_status(commit=False)` exists for exactly this reason. Pool is `pool_size=2, max_overflow=3`. Confirmed. |
| **C3** | P1 | Progress writes need **attempt ownership**, not just a terminal guard. `_on_progress` writes ORM attributes and commits unguarded, so a stale attempt can overwrite a replacement attempt's counters. | `tasks.py:621-634`. Confirmed. |
| **C4** | P1 | **The watchdog can undo a cancellation.** It SELECTs, then mutates ORM objects and commits **by primary key with no status precondition**. A cancel committed in that window is overwritten `cancelled` -> `pending` and re-enqueued. | `scheduler_helpers/health.py:92-215`. Confirmed. **Pre-existing production bug, not caused by this work.** |
| **C5** | P1 | Scrape percentage is not job percentage. A page denominator measures the scrape only; the prod run spent 401s of 522s there. Showing its 100% as whole-run completion still violates the no-fake-percentage rule. | Confirmed against the prod timeline. |
| **C6** | P2 | `next_retry_at` would be a promise the system breaks: the watchdog's stranded-retry branch keys on `Job.created_at < 70min`, not the retry time, so it can redispatch during the backoff. | `health.py:119-131`. Confirmed. |
| **C7** | P2 | The stage list describes a pipeline that does not exist. Export precedes enrichment; scrapers do their own parcel lookup; skip-trace only enqueues; delivery happens **after** `done` and its SSE event, so a guarded `delivering` stage would be rejected and the stream may already be closed. | `tasks.py:1261 -> 1404`, `:2244`. Confirmed by the prod log. |
| **C8** | P2 | King's denominator is wrong at boundaries: `max(1, days // 90 + 1)` reports 2 chunks for exactly 90 days while the loop runs 1; a same-day range reports 1 and runs 0. Failed chunks are skipped, so `chunk_num` counts failures as completed. | `king_wa_probate.py:165, 197-232`. Confirmed. |
| **C9** | P2 | Stage-only SSE events do not carry counter changes, and Pub/Sub is not replayable: reconnect needs an authoritative snapshot and late events must not overwrite it. | Design point, accepted. |
| **C10** | P2 | The SSE premise was outdated: this tree uses `sse_leases:{user_id}` with 20s renewal / 60s TTL, not `sse_conn`/`sse_count`. | `src/api/sse_leases.py`. My earlier read was the stale tree. Confirmed. |
| **C11** | P2 | Cancellation is terminalization, not interruption: the endpoint writes `cancelled` and does not revoke Celery, close Playwright, release the reservation, or publish an event. Riskiest point is **exporting after quota reservation** — the grant is charged and only the 5-minute sweep returns it. | `routes/jobs.py:332-363`. Confirmed. |

**Cut on Codex's advice:** whole-run percentage, whole-run ETA, `delivering` as a
scrape stage, and the assumption of a linear stage sequence.

---

## Revised plan

Hard rule: a percentage renders only with a real denominator, and it is **scoped and
labelled to the activity it measures**. No whole-run percentage. No elapsed-to-percent.
No auto-increment. No cap, no freeze. `done` is a completion state, not "100%".

### Phase 1 — BE: make UNKNOWN representable, per fact (2 files)
- [ ] Migration, all nullable, no backfill (NULL = unobserved). Deployed **before** any worker change so old workers simply report unknown.
  - `stage` varchar, `stage_started_at` timestamptz
  - `records_found` int — raw scrape total, **never** overwritten by billing (C1)
  - `units_done` int, `units_total` int, `progress_unit` varchar (`page|chunk|parcel|record`) (C1, C8)
  - `last_progress_at` timestamptz — progress-granularity liveness, not the UNKNOWN sentinel
  - `next_retry_at` timestamptz — a **not-before** target, never a guarantee (C6)
- [ ] `src/db/models.py` columns plus comments naming the invariants.

### Phase 2 — BE: attempt-scoped, commit-safe progress writes (3 files)
- [ ] `_set_progress()` in `tasks_helpers/status.py`: one guarded UPDATE on
      `(id, started_at == this attempt, status NOT IN terminal)`. Rowcount 0 means
      superseded: write nothing, publish nothing (C3).
- [ ] Stage changes ride the transaction boundaries that already exist — `_set_status`
      and the `_publish_log(db=db)` commits — so no new `commit()` is introduced on the
      work session (C2). Publish only after commit.
- [ ] `_on_progress` moves onto `_set_progress` and stamps `records_found`,
      `units_done/total/unit`, `last_progress_at`.
- [ ] `_retry_scrape_job` + the watchdog reset the new observations and stage atomically
      with the counters they already reset (C6).
- [ ] Stages are **real boundaries and may repeat**: `preparing, connecting, searching,
      scraping, saving, deduping, exporting, enriching, queuing_contacts, finalizing`.
      No `delivering` (C7).

### Phase 3 — BE: the API stops lying (3 files)
- [ ] `JobResponse`: `records_found`, `units_done`, `units_total`, `progress_unit`,
      `stage`, `stage_label`, `stage_started_at`, `next_retry_at` — every one nullable
      and independently unknown.
- [ ] `stage_progress_pct` only when `units_total > 0 AND units_done > 0`, explicitly
      scoped to `stage`+`progress_unit`. Whole-run `progress_pct` retired from the
      contract (C5). Revisit `min(99, ...)` and `done -> 100`.
- [ ] `estimated_time_remaining` only for a stage with a real denominator and >= 2
      observations, labelled for that stage.
- [ ] SSE: emit `{"type":"progress"}` carrying a **full snapshot** (stage + counters),
      not a stage-only ping; the FE re-syncs from the REST snapshot on reconnect and
      ignores older snapshots (C9). Add the 15s keepalive into the existing lease-aware
      deadline loop without disturbing renewal or the 30-minute expiry (C10).

### Phase 4 — BE: normalize the county adapters (2 sub-phases, ~5 files each)
- [ ] 4a: `base_scraper.report_stage()`; King probate derives the denominator from the
      **actual windows**, distinguishes attempted from completed chunks, reports the
      total before chunk 1, and reports `connecting`/`searching` (C8). Wire the callback
      **before** browser context entry so startup is observable (C7).
- [ ] 4b: the other 8 adapters report honest unknown totals with a correct
      `progress_unit`; fix `king_wa_tax_delinquent.py:512` zeroing the record count;
      stop Pierce reporting parcel counts as pages.

### Phase 5 — BE: tests + contract
- [ ] The 20 states, plus the race/failure cases Codex named: cancel vs watchdog
      recovery, stale callback vs new attempt, publish failure, reconnect mid-transition,
      failed retry publication, empty-but-successful scrape, exact chunk boundaries, and
      57-found vs 2-billed.
- [ ] Assert UNKNOWN never serializes as `0`.
- [ ] Regenerate `schema/openapi.json` in `.venv-schema`, diff vs `origin/main`.

### Phase 6 — FE: the status card (4 files)
- [ ] Activity is the headline. Percentage is secondary, scoped, and only when real.
- [ ] Indeterminate indicator; `prefers-reduced-motion` alternative.
- [ ] Metrics read "Searching..." while UNKNOWN; `0` only on an observed zero.
- [ ] ELAPSED vs ETA labelled by what we actually have.
- [ ] Retry: attempt N of M, countdown from `next_retry_at` worded as **not before**.
- [ ] LIVE / RECONNECTING / WAITING / CONNECTION LOST from the hook's real state.
- [ ] `role="status"` + `aria-live="polite"` on stage transitions only; timer `aria-hidden`.

### Phase 7 — FE: mobile + verification
- [ ] Playwright against a stub API at 320 / 375 / 390 / 430 and desktop, all 20 states.

### Phase 8 — Review
- [ ] Codex `review` + `challenge` on both diffs; verify every finding myself.
- [ ] Security Master Review (§14) twice clean.
- [ ] `docs/BUILD_JOURNAL.md` entry.

---

## Open decision

**C4 — the watchdog can resurrect a cancelled job.** Live bug, pre-existing, adjacent to
this work rather than caused by it. A cancelled run can be flipped back to `pending`,
re-enqueued, re-scraped, re-billed and re-delivered. The user asked explicitly that
cancellation not leave duplicate workers or a non-terminal job, so it is in scope by
intent, but it is its own fix with its own blast radius.

---

## Review

### Shipped

| PR | State | Gate |
|----|-------|------|
| BE **#347** watchdog guarded recovery writes | **MERGED** `8ba7bf8`, deployed | CI full suite green; Codex reviewed and found a hole in the first version, fixed in commit 2 |
| BE **#348** truthful progress | DRAFT | CI full suite + lint + OpenAPI drift green; **Codex gate INCOMPLETE** |
| FE **#158** Live Run UI | DRAFT | tsc + eslint + next build clean; browser-verified; **Codex gate INCOMPLETE** |

### The Codex gate did NOT complete

`codex review` on both diffs was interrupted by an OpenAI usage limit before it
produced any verdict:

```
ERROR: You've hit your usage limit ... try again at 4:02 AM.
Review was interrupted. Please re-run /review and wait for it to complete.
```

Per `.claude/rules/codex-collaboration.md` a build is not cleared until Codex has
reviewed the diff. **Neither #348 nor #158 has been.** Both stay DRAFT. Re-run:

```
cd C:/Users/Windows/bl-wt-liverun     && codex review --base main
cd C:/Users/Windows/bl-wt-liverun-fe  && codex review --base master
```

Codex DID review the PLAN (before any code) and the #347 diff; both of those
produced findings that were verified and folded in. It is only the two final diff
reviews that are missing.

### Security Master Review (§14) — RUN, two passes, clean

Translated to this stack per `.claude/rules/security.md`. Checked against the diff,
not asserted.

| # | Category | Finding |
|---|----------|---------|
| 1 | Authorization | CLEAN. `JobCreate` accepts only `scraper_config_id` + `trigger`; **none of the 8 new columns is client-writable**. All job reads go through ownership-filtered queries (9 sites with `Job.user_id == current_user.id`). |
| 2 | Secrets | CLEAN. None added. |
| 3 | Input validation | CLEAN by construction: the new fields are output-only. `stage` / `progress_unit` are free text at the DB but only the worker writes them, from `JOB_STAGES` / `JOB_PROGRESS_UNITS`. |
| 4 | Error handling | CLEAN. `_set_progress`'s except logs field NAMES only, never values, and returns False rather than surfacing anything. No new `HTTPException` detail. |
| 5 | XSS | CLEAN. `stage_label` is composed server-side from fixed dicts plus integers; an unrecognised `stage` returns None and falls through to status wording, so even a poisoned column cannot emit arbitrary text. Rendered as a React text child. 0 `dangerouslySetInnerHTML`. |
| 6 | SQL injection | CLEAN. `update().values(**kwargs)` is parameterized with keys constrained by a TypedDict; `make_interval(secs => :cd)` is a bound param, not interpolation. |
| 7 | File uploads | N/A. |
| 8 | Rate limiting | CLEAN. No new endpoint. The SSE keepalive adds ~13 bytes / 15s / stream, bounded by the existing 5-stream lease and 30-min cap. |
| 9 | CSRF | CLEAN. No new routes. |
| 10 | PII | CLEAN. Stage name, integer counts, timestamps. No PII. |
| 11 | Configuration | CLEAN. No new table, so no new RLS policy; columns inherit `jobs`' RLS. Migration additive + nullable with `lock_timeout`. |
| 12 | Dependencies | **FINDING — fixed.** See below. |
| 13 | Logging | CLEAN. New warn paths log identifiers and field names, not values. 0 `console.log` added. |
| 14 | Non-negotiables | CLEAN. No user_id filter dropped; no new navigation (SSRF untouched); none of the new fields reach an export path (CSV injection); no secrets; no error silenced as a fix (the two broad excepts are telemetry-only, logged, documented). |

**Finding (cat. 12, Medium, FIXED):** the FE branch was cut from `master` @ `6c435d0`
and master had since moved one commit ahead — **#159 `10d65d7`, "next 16.3.5 + pinned
next-auth, 30 vulnerabilities to 0"**. `git diff master..HEAD` rendered that bump as a
*reversion* on my branch, i.e. merging would have rolled back a security upgrade.
Rebased onto it; the diff is now exactly the 4 intended files. Re-verified against the
upgraded tree (`npm ci`, 849 packages): tsc, eslint and `next build` all clean.

This is the finding that justifies the rule. Nothing in the feature work would have
surfaced it; only running the review did.

### Connector stage gap — CLOSED

All 10 connectors now call `report_stage()` (was 3). `tests/test_scraper_progress_reporting.py`
walks the registry's own module allowlist, so a connector added later is covered
without anyone remembering the test. Verified non-vacuous: removing one call fails it
by name. Targeted suite: 1332 passed, 1 skipped.

### Not done

- **Codex diff-review gate still OPEN.** Re-probed after the gap fix; still
  rate-limited (`try again at 4:02 AM`). Neither PR is cleared.
- Local full-suite run was reaped for low memory at 78%; CI covered it instead.

### Deploy order

**#348 must merge and deploy before #158.** The page reads fields that ship in the
backend. Migration 099 is additive and safe to deploy ahead of the worker: old
workers leave the new columns NULL, which reads as UNOBSERVED.
