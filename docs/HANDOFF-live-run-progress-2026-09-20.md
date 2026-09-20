# HANDOFF — Live Run progress: "0% for 8 minutes of a healthy run"

**Date:** 2026-09-20
**Status:** Both PRs open as DRAFT, CI green, Codex gate passed once and re-review pending.
**Owner decision needed:** re-run the Codex gate on the corrected diff, then undraft and merge.

---

## 1. The goal

The Live Run screen showed a giant **`0%`** with **`Records 0 / Pages 0`** for 5+ minutes
while the backend log stream clearly showed the job working. The owner's brief: fix both
the UX and the root cause, and **never fake progress** — no elapsed-to-percent, no
auto-increment, no 95%-and-park. Unknown must be distinguishable from zero **in the data
model**, not just visually. Also: do not hide a genuinely stuck scraper behind a nicer
animation.

## 2. What was actually wrong (verified in production, not inferred)

Prod job `b80bd9a5-c5f7-4239-9519-71eb8fbc4fa3` (King WA probate, manual, `status=done`,
**57 records scraped, 2 billed**) ran 8m42s:

```
21:54:39  Connecting to county portal...
          <-- 401s: no log, no status change, page_current=0, page_total=0, record_count=0
22:01:20  Scrape complete: 57 records found
22:01:29  Looking up county records for 48 properties...
          <-- another 97s
22:03:07  Found 48/48 mailing addresses
22:03:19  done.        heartbeat alive at 22:02:40
```

**8m18s of an 8m42s run had nothing measurable. The run was healthy, not stuck.**

Three causes:

1. **UNKNOWN was not representable.** `jobs.page_current / page_total / record_count` are
   `Integer NOT NULL DEFAULT 0`, so "not measured yet" and "measured, found nothing" are
   the same value.
2. **Nothing was reporting.** `status` goes to `scraping` and says nothing more for the
   whole scrape; `enriching` then covers save, dedup, export, address lookup and contact
   queueing at once.
3. **The frontend made it visible.** The ring was fed `progress ?? (isRunning ? 5 : 0)`
   while the NUMBER inside it was fed `progress ?? 0` — a 5% arc over a "0". Three of the
   five branches computing `progress` were unmeasured numbers: a hardcoded **90** through
   enrichment, a **`Math.min(85)`** cap, and a **0** fallback.

## 3. Branches / worktrees / PRs

| | Backend | Frontend |
|---|---|---|
| Repo | `web-scrapper-automation` | `bridgeleads-web` |
| Worktree | `C:/Users/Windows/bl-wt-liverun` | `C:/Users/Windows/bl-wt-liverun-fe` |
| Branch | `feat/live-run-progress` | `feat/live-run-progress` |
| Base | `main` | `master` |
| PR | **#348** (DRAFT) | **#158** (DRAFT) |
| CI | Test SUCCESS, mergeable CLEAN | see §7 |

**Already MERGED and deployed:** BE **#347** `8ba7bf8` — the watchdog fix (below).

> Both branches have been rebased onto their moving bases more than once. **Always
> `git fetch && git rebase origin/<base>` before judging the diff** — `git diff base..HEAD`
> on a stale branch renders the base's newer commits as *reversions*, which nearly made me
> revert someone else's security bump. It has bitten twice this session.

## 4. Commits

**Backend (`origin/main..HEAD`, 7 commits, HEAD `e0849d3`):**
```
e0849d3 fix(jobs): a counter belongs to the activity that produced it     <- Codex round 2
b7...   docs(todo): security Master Review results, and the dep bump it caught
...     feat(scrapers): every connector says what it is doing, not just three
...     docs(journal): the Live Run 0%, and the two reviews that caught my own bugs
...     feat(scrapers): make every connector report progress the same way
...     feat(jobs): keep the live stream honest about being alive, and publish the contract
...     feat(jobs): record what a run is actually doing, so unknown stops reading as zero
```

**Frontend (`origin/master..HEAD`, 3 commits, HEAD `788c5ef`):**
```
788c5ef fix(live): the silence watchdog fought the hidden-tab release      <- Codex round 2
11a03b8 fix(live): three truthfulness defects the browser pass found
4599a0e feat(live): show what the run is doing, not a percentage nobody measured
```

## 5. Active files

**Backend**
- `alembic/versions/098_job_progress_observations.py` — NEW. 8 nullable columns on `jobs`:
  `stage`, `stage_started_at`, `records_found`, `units_done`, `units_total`,
  `progress_unit`, `last_progress_at`, `next_retry_at`. NULL = UNOBSERVED. Additive,
  deploy-safe ahead of the worker.
- `src/config/constants.py` — `JOB_STAGES`, `JOB_PROGRESS_UNITS`.
- `src/db/models.py` — the new columns + the invariants in comments.
- `src/api/schemas.py` — `JobResponse`: new passthrough fields, `_stage_label()`,
  activity-scoped `progress_pct`, the legacy fallback (gated on `stage IS NULL`).
- `src/workers/tasks_helpers/status.py` — `_set_progress()`, `_set_stage()`,
  `next_retry_at` in `_retry_scrape_job`, `next_retry_at=None` on claim.
- `src/workers/tasks.py` — 8 stage boundaries, `_on_progress` rewrite, `_on_stage`,
  `attempt_started_at` local.
- `src/workers/tasks_helpers/enrich.py` — callbacks wired BEFORE `__aenter__`.
- `src/workers/scheduler_helpers/health.py` — `_Candidate`, `_recovery_cas`, resets the
  new observations on re-queue.
- `src/scrapers/base_scraper.py` — `report_stage()`, `chunk_windows()`, `ProgressCallback`
  gains `record_count: int | None` + `unit`.
- All 10 connectors under `src/scrapers/`.
- `tests/test_job_progress_observations.py` (NEW), `tests/test_scraper_progress_reporting.py`
  (NEW), `tests/test_job_response_liveness.py`, `tests/test_king_cv_sources.py`,
  `tests/test_workers.py`.
- `schema/openapi.json` — regenerated, **113 insertions / 0 deletions**, purely additive.

**Frontend**
- `app/(dashboard)/live/[id]/page.tsx` — the rewrite.
- `components/ui/indeterminate-ring.tsx` — NEW.
- `hooks/use-log-stream.ts` — keepalive tracking + silence watchdog.
- `lib/api-types.generated.ts` — regenerated, 20 insertions / 0 deletions.

## 6. The design, and what was deliberately CUT

**Kept:** a percentage renders only from a real denominator, and is **scoped to the current
activity** (`stage_label`), never the whole run. Estimates need **two** completed units —
the first unit of a scrape carries all the browser startup and captcha cost.

**Cut, on Codex's advice, before any code was written:**
- whole-run percentage and whole-run ETA (the only denominator any connector produces
  measures the scrape, which was 401 of 522 seconds on the traced run);
- `delivering` as a stage (email/webhook dispatch happens AFTER `done` and after the
  terminal SSE event, so a stage write there would be refused and have no stream to reach);
- the assumption that stages are linear — **the CSV export runs BEFORE enrichment**. Never
  derive "step N of M".

**No progress event on SSE**, deliberately. The page already polls an authoritative,
replayable snapshot every 3s; a second non-replayable channel only adds the ordering hazard
where a late Pub/Sub message overwrites a fresher REST read on reconnect. SSE got a **15s
`:` keepalive** instead, which is what makes the LIVE dot mean something.

## 7. Current state / what is green

- **BE #348:** CI Test SUCCESS, dependency audit SUCCESS, lint, app-import and OpenAPI
  drift gate all green. Targeted suites green (53 across the three progress files; 1332
  passed on the broad scraper/job selection).
- **FE #158:** `tsc --noEmit`, `eslint`, `next build` all clean against **next 16.3.5**.
  Its `check` job FAILS, and that is **expected**: the api-types drift gate regenerates from
  the backend schema on `main`, which does not have the new fields until #348 merges. It
  goes green on merge. **Do not "fix" it by reverting the types.**
- Browser-verified across **15 states × 320/375/390/430/1280**: 0 horizontal overflow, 0
  leaf-text collisions, Cancel present and in-viewport for all five cancellable statuses.

## 8. Failed attempts / dead ends / traps (read this before repeating them)

1. **I started against a 141-commit-stale checkout.** The Desktop repo was far behind
   `origin/main` and the FE checkout was on a divergent branch. Everything read in the
   first 30 minutes was wrong. **Work from a fresh worktree off `origin/<base>`.**
2. **`git diff base..HEAD` on a stale branch lies.** Twice it showed the base's newer work
   as reversions — once it looked like my `next build` had corrupted `package-lock.json`,
   when in fact `master` had landed a security bump (#159, next 16.3.5, 30 vulns to 0) and
   my branch merely predated it. **Rebase first, then read the diff.**
3. **I nearly merged #347 with its fix unpushed.** Commit 2 was never pushed; the PR head
   was commit 1 and CI was green on *that*. Caught by diffing local HEAD against the PR
   head at the merge step. **Check the PR head SHA, not your local one.**
4. **`ln -s` on Windows deep-copies** and the process outlived `TaskStop`, locking a partial
   `node_modules`. Use `cmd /c mklink /J`. Better: `npm ci` in the worktree, because a
   junction to the main repo's `node_modules` means you build against the WRONG dependency
   versions.
5. **`.venv-schema` is broken** — its python still points at the removed anaconda. Regenerate
   `openapi.json` with `C:/Users/Windows/bl-rescat-venv/Scripts/python.exe` and verify the
   diff is additive-only.
6. **Killed background tasks leave the real process running.** Both a reaped pytest and two
   reaped `codex review` runs kept going and kept writing. Check for orphans; the Codex
   verdicts actually landed *after* the task was reported killed.
7. **DOM probes lie if written carelessly.** `AnimatedCounter` renders a 0-9 digit reel, so
   grepping leaf text for "0" false-positives on every counter. Inline `<span>` reports
   `clientWidth` 0, so `scrollWidth > clientWidth` always reads "clipped". And a 1.2s wait
   after `goto` is not enough for the job query — assert on a state marker, not on time.
8. **Local full-suite runs get reaped for low memory.** CI is the authoritative gate.

## 9. Bugs found in other people's code along the way

- **BE #347 (MERGED `8ba7bf8`) — the watchdog could undo a Cancel Run.** It SELECTed stuck
  jobs then committed by primary key with no status precondition, so a cancel landing in
  that window was flipped `cancelled` -> `pending` and re-enqueued, re-scraped, re-billed
  and re-delivered. Now a guarded CAS on (status, started_at, retry_count) as observed,
  using a frozen `_Candidate` namedtuple — **because `Session.rollback()` expires loaded ORM
  objects regardless of `expire_on_commit=False`**, which Codex caught and I reproduced.
- **King probate's chunk denominator** was `max(1, span // 90 + 1)` against a separate
  `while` loop. Those disagree at every exact multiple of 90 — **and `rolling_90` is exactly
  90** — so the most common config could never show progress past 50%. Both now derive from
  `chunk_windows()`.
- **`record_count` is NOT a scrape total.** The done-CAS overwrites it with the BILLED
  non-duplicate count. That is why the traced run scraped 57 and stored 2. Use
  `records_found`.

## 10. Reviews

**Codex reviewed the PLAN before any code** and rejected the first data model (one shared
sentinel; `records_found` aliasing `record_count`). Five P1s, all verified against the tree
and the prod row, plan rewritten.

**Codex diff review, round 2 — 5 findings, all resolved:**

| # | Sev | Finding | Outcome |
|---|-----|---------|---------|
| BE-1 | P2 | Counters leaked across activities: a finished 5/5 scrape made enrichment read "Part 5 of 5" at 99% with a 0s ETA | FIXED — `_set_stage` clears them; legacy fallback gated on `stage IS NULL` |
| BE-2 | P2 | Stage never advanced past `searching`; Pierce counted parcels while still labelled searching | FIXED — phase carries the stage in the same guarded write, moves only on a real transition |
| FE-1 | P1 | "build-blocking TS7053 on the unit lookup" | **NOT REPRODUCED.** `strict: true` and `ignoreBuildErrors: false` are as described, but `tsc --noEmit` and `next build` are clean. Hardened to `Record<string, string>` anyway |
| FE-2 | P2 | Silence watchdog defeated the hidden-tab release, holding a stream slot | FIXED |
| FE-3 | P2 | Terminal runs kept saying "Searching" | FIXED — "Not reported" |

**Security Master Review (§14): run, two passes, clean on 13 of 14.** Category 12 caught
that the FE branch predated the security dependency bump on master (see trap #2).

## 11. NEXT STEP

1. **Re-run the Codex diff gate on the corrected diff.** Round 2 reviewed the code *before*
   the five fixes above. Both branches have moved since.
   ```
   cd C:/Users/Windows/bl-wt-liverun     && codex review --base main
   cd C:/Users/Windows/bl-wt-liverun-fe  && codex review --base master
   ```
   Note: the verdict is written to **stdout**; the long trace goes to stderr. A reaped task
   may still finish — check the output file before concluding it failed.
2. **Undraft and merge BE #348.** It must deploy BEFORE the frontend.
3. **Undraft and merge FE #158** once #348 is deployed; its `check` job turns green on its
   own at that point.
4. **Verify against a real county run.** Nothing has been proven against a live scrape since
   deploy — the evidence so far is a stub API, CI, and the prod read of the original job.
   A King probate run should now show: "Connecting to the county records system" with an
   indeterminate ring and "Searching" tiles, then "Collecting records: Part N of M" with a
   real percentage, then "Adding property and mailing details" with the counters CLEARED.
5. **Optional follow-up:** `docs/security/SECURITY_PROMPT_PACK.md` §15 (Pre-Launch) before
   any production deploy, per the standing rule.
