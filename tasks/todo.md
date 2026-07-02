# Fix: "Fields to collect" checkboxes are cosmetic → make them functional at the output boundary

## Problem (verified in code, not assumed)
The new-scraper wizard's "Fields to collect" checkboxes are 100% cosmetic. The selection is
validated (`FieldsConfig`, `src/api/schemas.py:354`) and persisted (`scraper_configs.fields`,
`src/db/models.py:228`), but **no worker or export code ever reads `config.fields`**. Every
field is always scraped, stored, and written to the delivered CSV.

Verified load-bearing map: `parcel_id`, `party_name`, `property_address`, `mailing_address`,
`date_recorded` are consumed by enrichment / property_key / dedup / billing / skip-trace and
**cannot** be dropped at scrape/storage time. Only `heirs` (pure display) and
`legal_description` (only feeds the within-job idempotency fingerprint) are safe to suppress
at output.

Skip-trace mapping was also checked: frontend "Skip trace" → top-level `skip_trace_enabled`
(the field the worker honors). **No bug there** — verified, no change needed.

## Decisions (user + Codex consult, 2026-06-22)
- **Output-boundary filtering**, never gate scrape/enrichment on `config.fields`. (Codex + Claude agree.)
- **Force identity fields ON** — only `mailing_address`, `heirs`, `legal_description` are hideable.
  The 4 identity fields (`party_name`, `parcel_id`, `property_address`, `date_recorded`) always export.
- **Blank values, keep headers** — do NOT drop columns (stable schema for dialer/webhook consumers).
- **Empty/None/list `fields` → hide nothing** (legacy/default = all visible). Only an explicit
  `False` on a hideable field hides it.
- One **shared projection function** used by every export path (no per-path rules).

Codex session: `019ef0c7-1e11-7463-abca-e74018bfc2f6` (saved for follow-ups).

## Phase 1 — single-job export (the 95% path)  [<=5 files]  ✅ DONE (commits af13b51, 30602f0)
- [x] `src/utils/lead_export.py`: `HIDEABLE_OUTPUT_FIELDS`, `resolve_hidden_output_fields`,
      `_apply_visibility`; `write_lead_csv` gains `hidden_fields`.
- [x] `src/utils/data_exporter.py`: threaded `hidden_fields` through to_csv/to_excel/to_json/export.
- [x] `src/workers/tasks.py:780,995`: pass `resolve_hidden_output_fields(config.fields)`.
- [x] `src/api/routes/jobs.py`: load job's `ScraperConfig.fields` (owner-scoped), pass to `write_lead_csv`.
- [x] Verify: py_compile + ruff clean; 75 export tests pass (+12 new). Codex review: gate PASS.
      P1 (batch-child) verified theoretical (Job.scraper_config_id NOT NULL + children carry fields);
      P2 (export-format tests) addressed.

## Phase 2 — batch combined export  ✅ DONE (commit e7dc5c4)
- [x] `src/utils/lead_export.py`: threaded `hidden_fields` into `write_lead_csv_with_overlap` /
      `build_overlap_export_row` (reuses `_apply_visibility`).
- [x] `src/workers/batch_export.py`: `finalize_batch_run` resolves `ScraperBatch.fields`;
      `render_combined_csv` accepts + threads `hidden_fields`.
- [x] `src/api/routes/batches.py`: both download routes pass `batch.fields` via `_stream_run_csv`.
- [x] Verify: py_compile + ruff clean; 77 export/batch tests pass (+2 new). Codex review: NO findings.
- Note: the multi-config Lists/segments overlap export (`segments.py:111`) intentionally stays
  show-all (no single config => default `hidden_fields=None`); backward compatible.

## Follow-up (frontend repo `bridgeleads-web`, separate — note only)
- [ ] Lock/disable the 4 identity checkboxes (they're now intentionally non-hideable).
- [ ] Relabel "Fields to collect" → "Fields to include in export" (Codex: UI lies about scope;
      backend semantics are output-visibility, not collection).

## Review
**Done (2026-06-22). Both phases shipped to local branch `feat/fields-output-visibility`
(off origin/main), 3 commits: af13b51, 30602f0, e7dc5c4. NOT pushed — awaiting user.**

What changed: the wizard's "Fields to collect" checkboxes are now functional at the OUTPUT
boundary across every lead-export path (live download, scheduled/R2 + emailed delivery, batch
combined download + delivery; csv/json/excel). config.fields is NEVER consulted before the
output boundary, so scraping/enrichment/dedup/billing/skip-trace are untouched (5 of 7 fields
are load-bearing upstream). Only `mailing_address`/`heirs`/`legal_description` are hideable;
the 4 identity fields are force-on. Suppression blanks the value and keeps the header.

Verification: py_compile + ruff clean on all 7 touched files; 77 export/batch tests pass
(+14 new). Codex reviewed both phases — Phase 1 P1 (batch-child) verified theoretical +
P2 (format tests) addressed; Phase 2 came back with zero findings.

Not done / by design:
- The skip-trace mapping was checked and is already correct (frontend → `skip_trace_enabled`).
- FRONTEND follow-up still required (separate `bridgeleads-web` repo): lock the 4 identity
  checkboxes + relabel "Fields to collect" → "Fields to include in export". Until then the UI
  still shows 4 checkboxes that the backend intentionally ignores (narrowed from all 7).

---

# Schedule Wizard Verification (2026-06-22) — Q1 of 3

User asked: on the new-scraper Schedule step, do Frequency / Run time / Date range
actually do what the user picks? Working through 3 questions one at a time, each
cross-checked with Codex.

## Q1 — Frequency (Manual / Daily / Weekly / Monthly)  ✅ DONE + Codex GO

Findings (traced `dispatch.py` + `schemas.py`, Codex-confirmed):
- [x] Manual — correct. Never auto-fires; runs on demand via POST /jobs.
- [x] Daily — correct. Fires daily at chosen UTC time.
- [x] Weekly / Monthly — WERE hardcoded to Monday / the 1st. Root cause: `ScheduleConfig`
      had no weekday / day-of-month field, so the user's day could not be stored.

Fix (user chose root-cause day picker):
- [x] `ScheduleConfig` + `ScheduleConfigDict`: added `run_at_weekday` (0=Mon..6=Sun) and
      `run_at_day_of_month` (1..31). OpenAPI `description=` documents the contract.
- [x] `_should_run_now` rewritten: typed ints, weekly matches chosen weekday, monthly
      matches chosen day clamped to month length (31 → last day of short months).
- [x] Defaults (Mon / 1st) reproduce old behavior → zero migration for existing configs.
- [x] Codex P1 fixed: module-level `_coerce_schedule_int` used by BOTH the matcher and the
      batch occurrence key, so corrupted JSON can't pass the matcher then crash now.replace().
- [x] Tests: `tests/test_should_run_now.py` (10) + `tests/test_schema_bounds.py` (+4). 28 pass, ruff clean.

Decision logged: weekday contract is 0=Monday (Python-native) not 0=Sunday — diverged from
Codex's mild preference because the dispatch default then reproduces old Monday behavior with
no conversion layer. Documented in schema + frontend spec below.

Known accepted limitation (pre-existing, not introduced here): the ±1-min window is not
midnight-wraparound-aware. Under a healthy beat 2 same-day ticks still match, so no occurrence
is missed unless the beat also drops both ticks. Documented in `_dispatch_due_batches`.

FRONTEND follow-up (separate `bridgeleads-web` repo) — REQUIRED for the picker to be usable:
- When Frequency = Weekly, show a weekday <select> sending `run_at_weekday` 0=Mon..6=Sun
  (explicit option values; do NOT send JS `Date.getDay()` raw).
- When Frequency = Monthly, show a day-of-month <select> (1..31) sending `run_at_day_of_month`;
  copy can note "29–31 run on the last day in shorter months."
- Schedule Summary should show the chosen day ("every Wednesday", "on the 15th") and should
  NOT show a run time for Manual (Manual ignores it).

## Q2 — Run time (UTC) hour/minute  ✅ DONE + Codex GO

Findings (traced + Codex-confirmed):
- [x] run_at_hour/run_at_minute ARE honored — _should_run_now gates firing at the
      chosen UTC time. Inputs bounded 0-23 / 0-59 at the schema. Manual ignores run
      time (correct); the "Runs Manual at 06:00 UTC" summary is frontend cosmetic.
- [x] REAL edge found: scheduled single-config jobs could DOUBLE-FIRE. The ±1-min
      window fires 3 beat ticks; the active-job-only dedup missed a duplicate when a
      fast scrape finished before the next tick. Batches were already safe via
      uq_batch_runs_occurrence; jobs had no equivalent.

Fix (user chose light / no-migration):
- [x] New `_scheduled_dispatch_blocker_exists(db, config_id, now)` — skips if a job is
      active (any trigger, overlap guard) OR a `scheduled` job was created within
      `_SCHEDULED_DEDUP_MINUTES` (3). Replaces the active-only check in the dispatcher.
- [x] trigger='scheduled' filter so a manual "Run now" neither suppresses nor is
      suppressed by the schedule. created_at is timestamptz; now is tz-aware UTC.
- [x] 5 DB-backed tests (tests/test_scheduled_job_dedup.py): no-jobs / active-any-trigger
      / recent-finished-scheduled-blocks / old-scheduled-allows / recent-manual-no-suppress.
      All 42 schedule tests pass, ruff clean.

Documented limitation (Codex, accepted): single-beat mitigation, NOT a concurrency
guarantee. Multiple concurrent beat schedulers could still race; the durable fix is a
(config, occurrence) unique key like the batch path. Out of scope for the no-migration fix.

## Q3 — Date range (Rolling 90 / Since last run / Custom)  ✅ DONE + Codex GO

Findings (traced + reproduced + Codex-confirmed):
- [x] Rolling 90: exactly 90 days for non-tax record types. For tax_delinquent it's
      18 months BY DESIGN (test_tax_delinquent_rolling_90_still_18_months locks it;
      90 days misses annual tax cycles). The mismatch is the UI LABEL only.
      DECISION: fix UI label only — NO backend change (see frontend follow-up).
- [x] since_last_run + custom could return an INVERTED window (date_from > date_to):
      same-day rerun, starter 7-day-delay edge, or a backwards custom range.
      Reproduced live (custom 6/30->1/1 passed through). Nothing downstream caught it
      (max-days trim only fires on too-LONG ranges).

Fix (user chose fix-both root-cause):
- [x] `_ordered_window` (dates.py): collapses any inverted window to a single day at
      date_to. Applied at BOTH resolver returns (custom early-return + final return),
      so since_last_run / starter-delay / custom are all covered by one choke point.
- [x] `ScheduleConfig.validate_custom_range` (schemas.py): rejects custom date_from >
      date_to at SAVE time with a clear error (suspenders; the resolver guard is the
      belt for legacy/direct-DB rows). `_parse_schedule_date` accepts ISO + US formats —
      verified identical to the worker's `_to_mmddyyyy` set, so no normalize drift.
- [x] Tests: _ordered_window unit + custom-inverted repro (test_resolve_date_range.py),
      accept/reject custom ranges ISO+US (test_schema_bounds.py). 66 related tests pass, ruff clean.

Documented minors (Codex, not fixed — out of scope): malformed custom date strings (not
just inverted) can still save and fall through; the frontend date picker prevents these.
since_last_run keys off last job's finished_at not its covered date_to (gap only for
custom/delayed jobs, fine for daily rolling).

---

## FRONTEND follow-ups (separate `bridgeleads-web` repo) — consolidated for all 3 Qs

Q1 (day picker):
- Weekly → weekday <select> sending `run_at_weekday` 0=Mon..6=Sun (explicit values, NOT JS getDay()).
- Monthly → day-of-month <select> (1..31) sending `run_at_day_of_month`; note 29–31 run last day in short months.
Q1/Q2 (copy):
- Schedule Summary should show the chosen day ("every Wednesday" / "on the 15th") and should
  NOT show a run time for Manual (Manual ignores it).
Q3 (labels + validation):
- For record_type = tax_delinquent, the "Rolling 90 days" option mislabels an 18-month window —
  relabel / annotate (e.g. "Rolling ~18 months (tax)") or show the real window.
- Custom range: client-side validate date_from <= date_to before submit (backend now also rejects).

---

# Deeper skeptical A/B testing — Q4 (frontend) + Q5 (backend gap)

## Q4 — Frontend day-picker wiring + summary  ✅ DONE + Codex GO
Repo: bridgeleads-web, NEW additive branch `feat/schedule-day-picker` (off feat/fields-export-ui-honesty).
Audit found the backend day-picker feature was COMPLETELY UNWIRED on the FE (run_at_weekday /
run_at_day_of_month absent from Zod, types, and POST body) + summary showed a time even for Manual.
Already-good (left as-is): custom date_from<=date_to Zod check matches our backend 422; tax_delinquent
auto-switches to a pre-filled 18-month custom range so "Rolling 90 days" is never shown to tax users.
Fixed (5 files):
- [x] _lib.ts: Zod run_at_weekday (0=Mon..6=Sun, .default(0)) + run_at_day_of_month (1..31, .default(1)).
- [x] ScheduleStep.tsx: weekly→weekday <select> (Monday→0; setValueAs:Number per Codex so z.number() gets
      a number), monthly→day-of-month input (1-31) + "29–31 run last day" note. Summary now day-aware and
      shows NO time for Manual ("Runs manually — only when you click Run").
- [x] page.tsx: defaults + all 3 POST bodies (test-run, save, batch).
- [x] lib/types.ts + lib/api-types.generated.ts: added the two optional fields (regenerate after backend deploy).
- [x] tsc --noEmit + eslint both clean. Codex review: no P1/P2 (applied its setValueAs hardening).
NOTE: not live-QA'd in a browser (needs full local stack) — recommend a dogfood pass before shipping.

## Q5 — since_last_run keys off covered date_to (not finished_at)  ✅ DONE + Codex GO (3 rounds)
src/workers/tasks_helpers/dates.py. Codex caught TWO P1s during review:
- P1a: must use last run's covered date_to, not finish timestamp (delayed/retried/starter-lag gap/overlap).
- P1b: must be the GLOBAL max covered date_to, not the most-recently-FINISHED job's date_to (a later
      backfill covering an older window would rewind the start and re-scrape months).
- Empirical: this Postgres is STRICT datetime mode — to_date('11/31/2026') RAISES, so a SQL to_date MAX
      would let one corrupt row poison the aggregate. Final fix reduces in PYTHON: fetch all done-job
      date_to for the config (one tiny column, no limit → global), strptime (rejects invalid → skips bad
      row), take max; fall back to latest finished_at, then 30 days. Final return still hits _ordered_window.
- [x] 5 DB tests: date_to path, finished_at fallback, no-job→30d, global-max-not-latest-finish, shaped-invalid-excluded.
Residual (documented, accepted): future date_to>end_date collapses to a single end_date day (guard, not no-op);
long-term schema fix = store date_to as a real date column.

## REMAINING (not blocking, documented):
- Frontend Q4 needs a live browser dogfood + (post backend-deploy) `npm run gen:api-types` regen.
- Backend nothing committed (working tree on feat/fields-output-visibility, shared with a concurrent session's
  fields-export work). Frontend on feat/schedule-day-picker (uncommitted).

---

# Final remaining items — Q6 (timezone) + Q7 (E2E dispatch)

## Q6 — UTC-vs-Pacific date boundary  ✅ INVESTIGATED — NOT A BUG (Codex agreed)
Codex earlier flagged a "likely timezone bug." On inspection it is an INTENTIONAL two-axis model:
- WHEN to run = UTC: dispatch timing + _should_run_now weekday/day matching are all UTC, matching the
  "Run time (UTC)" UI label.
- WHICH dates to scrape = US/Pacific: _resolve_date_range uses Pacific civil 'today' to match WA county
  record dates and deliberately avoid a +1 off-by-one (pre-existing comment).
These are orthogonal and each self-consistent: across daily runs the Pacific window advances exactly one
day/run (no gap/overlap); since_last_run resumes from the prior covered date_to (same Pacific axis). Only
effect = a UX nuance (a Pacific user's "weekly Monday 06:00 UTC" executes Sunday night local during PDT).
NO code change. ⚠️ Future caveat (Codex): hardcoded US/Pacific becomes a product-policy issue for national
expansion — county record dates should then use county/connector timezone.

## Q7 — E2E dispatch test + commit-then-enqueue fix  ✅ DONE + Codex GO
src/workers/scheduler_helpers/dispatch.py.
- [x] Extracted _dispatch_due_jobs(db, now) -> created ids from _dispatch_scheduled_jobs_impl (mirrors
      _dispatch_due_batches) so the dispatch glue is unit-testable with a fixed clock.
- [x] FIXED a latent ordering bug: the old _impl did db.flush()+run_scrape_job.delay() INSIDE the loop and
      db.commit() only AFTER it (enqueue-before-commit) — a worker consuming the message pre-commit gets
      rowcount=0 on the atomic pending->queued claim and strands the job 'pending'. Now: create+flush rows,
      commit, THEN per-item try/except .delay(). Watchdog (health.py) re-delivers committed fresh-pending as
      the documented backstop. Plain .delay() preserves prior routing.
- [x] 8 E2E DB tests (tests/test_dispatch_due_jobs.py): daily-due, manual-skip, not-due, weekly-weekday,
      record-limit, dedup-adjacent-tick (active branch), dedup-after-fast-finish (recent-scheduled branch =
      the real Q2 proof, per Codex), inactive-config. 44 scheduler/date tests pass, ruff clean.

## SESSION TOTALS (Q1–Q7)
Backend: dispatch.py, dates.py, schemas.py + 5 new test files (~30 new tests). Frontend: 5 files on
feat/schedule-day-picker. Every fix Codex-reviewed to GO (Q5 took 3 rounds; Q7 caught a latent ordering bug).
Nothing committed. Frontend needs a live browser dogfood + post-deploy api-types regen.

---

# Q8 — Independent cross-check + commit (2026-07-02)

Fresh session re-reviewed the whole uncommitted Q1–Q7 diff + ran a fresh ADVERSARIAL Codex pass on the
INTEGRATED result (prior Codex reviews were per-question). Verdict: solid, ruff-clean, all `_should_run_now`
callers updated, and Codex found NO bypass in the test-DB guard. Codex's 3 findings all map to already-
documented-accepted limitations:
- P1 concurrency race (read-then-insert dedup, no durable occurrence key) — DEFERRED per prior scope
  decision (needs a `(config, occurrence)` unique key + migration like batches' uq_batch_runs_occurrence).
  Does NOT trigger under a single Celery beat. User chose to keep deferred (no migration on shared repo).
- P2 ±1-min window not midnight-aware — already documented in `_dispatch_due_batches`.
- P2 since_last_run re-scans the last covered day when caught up — FIXED THIS SESSION (below).

Fix applied (Codex-approved verbatim, P2):
- [x] `_resolve_date_range` since_last_run: when `date_from (covered_to+1) > end_date` (caught up, or a
      custom/backfill covered into the future), clamp `date_from = end_date` EXPLICITLY + log it, instead
      of relying on `_ordered_window`'s inverted-collapse accident. Output-identical (downstream dedup
      already caught the redundant day); removes the hidden dependency so a future `_ordered_window`
      refactor can't silently break this path. +2 DB tests (caught-up, future-coverage) in test_since_last_run.py.
- [x] Removed a duplicated `## Q3` + FRONTEND-follow-ups block from this file (dead/duplicate content).

Committed to its own branch (additive, not pushed): test-DB guard as one commit, schedule-wizard feature
as a second commit, so the safety guard stays cherry-pickable. Concurrency P1 remains the one open item —
revisit if/when running >1 beat scheduler.
