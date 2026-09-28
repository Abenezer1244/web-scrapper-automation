# 2b-ii: per-scraper run eligibility + structured 402 bodies (UX audit Q6 / F-035)

Branch `feat/run-eligibility-2b-ii` from `origin/main` `ee601b55`, worktree
`C:/Users/Windows/bl-wt/eligibility`. Builds on 2b-i (`run_eligibility` in `src/api/quota.py`,
BE #369). Spec: FE `docs/ux-audit/phase-3.0-contracts.md` Q6.

## Facts (verified)
- **Prod `ENTITLEMENT_ENFORCEMENT` = `true`** on api AND worker (read 2026-09-27, one variable
  only). The code default is `False`. So `not_entitled` is a real BLOCK in prod, not a warning.
- `POST /jobs` → `enqueue_scrape_job` (`jobs.py:208`) refuses, in this order:
  1. `run_in_flight` 409 `{code, job_id, message}` (`_run_in_flight`, backed by index 104)
  2. entitlement 402 `{code, title, message}` via `enforce_runnable_http` / `plan_limit_http`
     (`config_run_violation`: record type not on plan, or county outside the plan's slots);
     audit-only when the flag is off
  3. AI monthly limit 402, **prose** string (`jobs.py:298-305`), CALENDAR month UTC
  4. account rule 402, **prose** string (`quota_block_reason`, 2b-i)
- `create_job` 404s an inactive config before any of that (`jobs.py:404-413`).
- `GET /scrapers` lists ONLY `active` configs (`scrapers.py:121`). An entitlement-paused config is
  `active=False, paused_reason='entitlement'` and never appears there; `GET /scrapers/{id}`
  returns it (no `active` filter). So `config_inactive` can only surface on the single GET.
- `ScraperConfigResponse` (`schemas.py:710`) carries no eligibility today.
- **AI classification is nondeterministic today (latent).** `enqueue` takes `.first()` of the
  active connectors for (state, county) with no ORDER BY and no mode / record-type filter, and the
  AI count joins on (state, county, mode='ai') only. The worker's real identity is (state, county,
  `record_type` in `record_types`, active) (`registry.py:72-95`). Prod read-only: 17 active AI +
  13 active manual connectors, **no county has both** active, 4 active configs sit in AI
  counties. So no customer is misclassified today, but one mixed county would make it random.
- `enqueue_scrape_job` has exactly one caller, `POST /jobs` (`jobs.py:415`); scheduler and batch
  paths go through `dispatch.py` / `batch_tasks.py` and are untouched here.
- `ix_jobs_user_created (user_id, created_at)` exists, which bounds the AI monthly count.
- FE `readErrorBody` (`lib/api.ts:603`) already takes `detail.message` from an OBJECT detail, and
  `toastError` routes every 402 to `toastUpgrade`, so a FROZEN account is offered "Upgrade",
  the wrong remedy. A structured `code` is what lets the FE fix that.

## Phase A (read side; no wire change to any refusal) — 5 source files
(`config_eligibility.py` new, `jobs.py`, `schemas.py`, `scrapers.py`, `registry.py`)
1. **New `src/api/config_eligibility.py`**: `async config_run_eligibility(db, user, configs, now)
   -> dict[config_id, ConfigRunEligibility]`, batched so the list costs a fixed number of
   queries, not N:
   - one query: the user's active config rows (entitlement slot math, as `enqueue` does today);
   - one query: jobs holding a run slot for these configs (`Job.holds_run_slot(now)`). Winner
     per config = `_run_in_flight`'s rule exactly: newest `created_at`; `stopping` iff its status
     is `cancelled`, which picks the "still stopping" message (Codex P2);
   - one query: the tenant's jobs since the UTC month start as (state, county, record_type) of
     their configs (only if the tenant has any AI-mode connector in reach; see below);
   - one query: ALL active connectors (manual AND ai) for the UNION of the configs' and those
     jobs' (state, county) pairs, so `pick_connector` sees exactly what the worker sees for every
     classified row (Codex P1, round 3);
   - `run_eligibility(user, now)` (2b-i) for the account.
   Precedence = the gate's order, so the reason shown is the 402/409 the user would get:
   `run_in_flight` → `not_entitled` → `ai_limit` → `frozen|ended|over_limit`.
   **`config_inactive` is PAGE-ONLY (Codex P1):** POST /jobs answers an inactive config with a
   plain 404 "Scraper not found" and keeps doing so; the evaluator reports `config_inactive`
   first for such a config on `GET /scrapers/{id}`, and the parity tests exclude it.
   `not_entitled` only blocks when `ENTITLEMENT_ENFORCEMENT` is on (else can_run, audit-logged by
   the gate as today). `ConfigRunEligibility` = `{can_run, code, message, resumes_at, job_id,
   violation_code}`:
   - `violation_code` (Codex P1) = the entitlement `Violation.code` the 402 carries today
     (e.g. `record_type`, `county_limit`, `plan_limit`), set only with `not_entitled`, so the page
     and the 402 name the same rule. `message` = the violation message.
   - `resumes_at`: `over_limit` → `next_quota_reset` (NULLABLE: null when the term ends at or
     before the window end, 2b-i rule; Codex P1); `ai_limit` → next UTC calendar month start;
     all others null. `job_id` only with `run_in_flight` (may be null: stopping job gone).
   - It keeps the `Violation` object internally so the gate raises its exact body.
   **Tenancy (Codex P1):** the in-flight query filters `Job.user_id == user.id` AND
   `Job.scraper_config_id IN (caller's config ids)`; the AI count filters `Job.user_id` and joins
   configs on `ScraperConfig.user_id == user.id`. Entitlement slot math uses ALL of the tenant's
   active configs, never the displayed subset (so `exclude_batch_children` cannot change it).
   **One clock (Codex P1):** `now` is captured once in `enqueue_scrape_job` / the route and passed
   to `holds_run_slot`, the AI month start, and `run_eligibility`.
   **AI identity (Codex P1, rounds 1+2):** ONE shared pure function
   `pick_connector(connectors, record_type)` (in `src/scrapers/registry.py`): among the active
   connectors for a (state, county), the first whose `record_types` lists the record type
   (case-insensitive), with connectors ordered by `(created_at, id)`. The worker's
   `get_scraper_for` uses it (its query gains `ORDER BY created_at, id`; today it has no ORDER BY,
   so a duplicate would be picked at random), and the evaluator uses it; a config is AI iff the
   connector the WORKER would run has `scraper_mode='ai'`. Prod read-only: no active duplicate
   (state, county, record_type) exists, so the worker's choice does not change today. The monthly AI count uses the
   SAME rule over the tenant's jobs since the month start (one query of (job id, state, county,
   record_type) via `ix_jobs_user_created`, classified in Python). The gate uses the evaluator, so
   gate and page cannot disagree. This fixes the latent `.first()` bug; since no prod county is
   mixed, no refusal moves in prod today (checked).
2. **`jobs.py` `enqueue_scrape_job` calls the evaluator** for `[config]` and raises from its
   answer, with today's EXACT bodies and statuses (409 run_in_flight dict, entitlement
   `plan_limit_http(violation)`, AI prose, account prose). One evaluator drives the gate and the
   page. The index-backed race path after the flush is unchanged. The evaluator returns data
   and never raises HTTP exceptions; only `enqueue_scrape_job` maps codes to responses (Codex P2).
   Its docstring's "manual + scheduled runs" is corrected: POST /jobs is its only caller (P3). The defensive "count the config
   toward its own county claim" row append is kept inside the evaluator.
3. **`schemas.py`**: `ConfigRunEligibilityResponse` (code Literal of the 7 codes, validator
   invariants like 2b-i, `job_id` only with run_in_flight) and
   `ScraperConfigResponse.run_eligibility: ConfigRunEligibilityResponse | None = None`.
4. **`scrapers.py`**: `GET /scrapers` and `GET /scrapers/{id}` fill it (one evaluator call for the
   whole list). Create / PATCH / csv-layout responses leave it null, documented as "not
   computed", never "allowed" (Codex P3).
   **Coverage limit, stated (Codex P1):** `GET /scrapers` still lists only active configs, so an
   entitlement-paused scraper stays invisible there; changing what the list returns is a separate
   UX change (queue item 4, F-043 Phase 2), not this one. Its eligibility is on `GET /{id}`.
5. OpenAPI regen (`bl-rescat-venv`, `--check`, structural diff: only the new component and the
   `ScraperConfigResponse` property).

## Phase B (402 envelope; separate PR, NEEDS OWNER DECISION) — not built in Phase A
Inventory of 402s that are still prose: AI limit (`POST /jobs`), account rule (`POST /jobs`,
`POST /batches`). Entitlement 402s (`POST /jobs`, `POST /scrapers`, PATCH, `POST /batches`) and
the run_in_flight 409 are already structured and stay as they are.
- **Recommended: additive, non-breaking (Codex P1).** Keep `detail` the SAME prose string and add
  top-level siblings: `{"detail": "<prose>", "code": "...", "resumes_at": ...}` via a small
  exception handler for one custom exception class. API-key customers reading `detail` as a string
  keep working; the web FE learns to read the top-level `code` (FE PR first, tolerant of both).
- Alternative (breaking): `detail` becomes an object like the entitlement 402. Rejected unless you
  prefer one shape over compatibility.
- Either way: declare the 402 body in OpenAPI with a shared model, and FE `readErrorBody` keeps
  `resumes_at` (it drops it today, Codex P2).
- Contract (Codex P2, round 3): `code` is exactly the evaluator's code (`ai_limit`, `frozen`,
  `ended`, `over_limit`); `resumes_at` exactly the evaluator's (`over_limit`:
  `next_quota_reset`, nullable; `ai_limit`: next UTC month start; else null); `detail` stays
  byte-identical prose. FE tests: parse the top-level shape, the nested entitlement shape, and a
  bare-string legacy body. Phase B gets its own plan + Codex round before it is built.

## FE follow-up (separate PR after BE)
Regen types; Run now disabled with `run_eligibility.message` (and "View live run" for
run_in_flight); 402 `code: frozen` → "Update payment", not "Upgrade".

## Tests (real DB, isolated `bridgeleads_eligibility_test`; each RED on unfixed code)
- evaluator per code, and precedence when several apply (in_flight beats not_entitled beats
  ai_limit beats account); `not_entitled` is can_run with the flag off, blocked with it on (the
  flag set per test via pytest `monkeypatch.setattr(settings, ...)`, as existing entitlement tests
  do; prod value is `true`); `violation_code` matches the 402's `detail.code` for record-type and
  county cases; ai_limit `resumes_at` = next UTC month start; over_limit on a cancelled term has
  `resumes_at` null.
- `pick_connector`: duplicate connectors for one record type resolve to the oldest, every time;
  the registry and the evaluator agree on a mixed and a duplicate county, including a manual-first
  duplicate (manual older than ai -> not AI).
- AI count across counties: an AI job in a county the page's configs do not cover still counts;
  a manual job in an AI-bearing county does not.
- in-flight winner: two slot-holding jobs on one config (active + recently cancelled) -> the newer
  wins, and a cancelled winner yields the "still stopping" message, matching the 409.
- create / PATCH / csv-layout responses carry `run_eligibility: null` (asserted contract).
- AI identity: mixed county (manual probate + ai tax) classifies each record type correctly and
  deterministically; a record type no connector lists is not AI; the AI count ignores the tenant's
  manual-connector jobs in the same county.
- batched: N configs issue a fixed query count (SQLAlchemy `before_cursor_execute` listener on the
  test engine), and slot math with `exclude_batch_children=true` equals the unfiltered answer.
- **parity**: for each scenario except `config_inactive`, `POST /jobs` status + body equal what
  the evaluator predicted, on one pinned clock.
- tenancy: a job row with `user_id` = another user but this user's `scraper_config_id` (a
  deliberately mismatched owner) is NOT reported in-flight for either user's page; another user's
  AI jobs never count.
- `GET /scrapers` / `GET /scrapers/{id}` carry the field; an entitlement-paused config on
  GET /{id} says `config_inactive`.
- perf: `EXPLAIN` of the in-flight and AI-count queries on the test DB uses the existing indexes
  (`uq_jobs_one_active_per_config` / `ix_jobs_user_created`); recorded in Review.
- existing gate tests unchanged and green (Phase A must not move a single refusal).

## Steps
- [ ] Owner confirms Phase A (and decides Phase B's API-key break).
- [ ] Tests RED → implement → GREEN; ruff; full suite (8 parts, isolated DB).
- [ ] OpenAPI regen + structural diff.
- [ ] Security review §14 (tenancy of the in-flight + AI count queries, no cross-user leak).
- [ ] Codex diff review on `origin/main...HEAD` until GATE: PASS.
- [ ] Quiesce, merge, prod verify (authed `GET /scrapers` shows `run_eligibility`).

## Codex consult
Round 1: NO-GO. P1s adopted: config_inactive page-only; `violation_code`; list coverage stated
(paused configs stay out of the list, separate UX item); tenancy on both new job queries incl. a
mismatched-owner test; deterministic AI identity (registry rule, fixes latent `.first()`); one
clock; nullable over_limit `resumes_at`; Phase B split out with a NON-breaking additive envelope
recommended. P2s adopted: all-tenant slot math, EXPLAIN check, `resumes_at` in `readErrorBody`,
flag-on tests. P2 not adopted: benchmark with large tenants / pagination: tenants are small
(7 users in prod), the list already returns every active config unpaginated today, and the new
work is a fixed 3-4 indexed queries; revisit with the national-scale plan. P2 "HTTP exceptions
leaking to scheduler": moot, `enqueue_scrape_job` has one caller (POST /jobs). P3 metrics
deferred.
Round 2: NO-GO. Most P1s restated "not implemented yet" (this is a plan review; the code is
unwritten by design). One real new P1 adopted: "any matching connector is AI" could disagree
with the worker's first-match choice. Now one shared deterministic `pick_connector` for worker
and evaluator; prod has no duplicate identities (read-only check), so no live change. P2 adopted:
the evaluator never raises HTTP. P3 adopted: docstring. Scale debt accepted as deferred, not as
proven (Codex's own framing).
Round 3: NO-GO. P1 adopted: the AI count classifies ALL counted jobs, so connectors are loaded for
the union of config and job jurisdictions, manual included. P2 adopted: in-flight winner rule =
`_run_in_flight`'s; Phase B contract spelled out. P3 adopted: null-field tests.
Round 4: **PLAN: GO**, no findings.

## Review
Built on `feat/run-eligibility-2b-ii` (rebased onto main `c9954f9f` after #370/#372).
- 34 real-DB tests (`tests/test_config_eligibility.py`). On unfixed code all RED; the AI-count
  bug RED behaviourally on the OLD gate (a Pro user with 50 MANUAL probate runs refused an AI tax
  run, 402 "50/50"). Mutation-proven: dropping `Job.user_id` from the in-flight query, dropping it
  from the AI count, removing the connector ordering, skipping the cross-county connector load.
- Full suite 4946 passed, 0 failed (pre-rebase; part 7 re-run after a LOCAL Postgres PANIC,
  "could not truncate file ... Permission denied", a Windows file lock, not the code). After the
  rebase: 213 related tests incl. the new `test_db_safety.py` green; CI runs the full suite.
- Prod EXPLAIN (no ANALYZE) of both job queries: index scans (`ix_jobs_user_id`,
  `ix_scraper_configs_user_county_state_type`) over 136 jobs.
- OpenAPI structural diff: `ConfigRunEligibilityResponse` + one property, nothing else.
- Security §14: no findings (owner-scoped job queries, auth unchanged, no new input/egress,
  refusal bodies byte-identical by the parity test).

Codex diff review:
- Round 1: GATE FAIL, no P1. P2 AI-count tenancy untested -> added (mismatched-owner job; the
  query also joins on the config's owner, so another tenant's own-config job is excluded twice);
  P2 entitlement parity compared only code/message -> whole body. P3 worker test added; P3 query
  bound documented (<= 6); inactive short-circuit not done (GET one config only).
- Round 2: GATE FAIL, no P1. Two P2s NOT adopted because the owner-approved plan decides them
  ("Phase A must not move a single refusal"), and both are pre-existing, identical in the old gate:
  (a) a config whose county no longer has an active connector for its record type reads
  can_run, and the worker fails it with UnsupportedCountyError; (b) past jobs are classified as AI
  from TODAY's connectors, so a connector mode change re-counts the month (a true fix needs a
  `was_ai` snapshot on `jobs`, a migration). Both queued below. P3s adopted: the worker test now
  proves which connector ran (the ai one would raise "no template"), and the query-bound test
  covers several counties plus a history-only county and asserts <= 6.

Follow-ups (queued, not in this PR):
- `connector_unavailable`: refuse (gate + page) a run no active connector can serve.
- Snapshot AI-ness per job (`jobs.was_ai`, migration) so the monthly count is immutable.
