# Duplicate scope and "already delivered" audit (2026-09-08)

Branch: `fix/duplicate-scope-audit` (BE), `fix/duplicate-scope-copy` (FE)
Worktrees: `~/bridgeleads-worktrees/dup-scope`, `~/bridgeleads-web-worktrees/dup-scope`

## Verdict on the report

**There is no cross-account leak.** Verified in code, in production data, and
live in the browser. But the page that made the owner suspect one is genuinely
broken: its "prove it to me" link sends you to the wrong run.

### Evidence

Account (`e73585c6`), created 2026-06-23, plan starter, limit 50:

| when | job | county/type | results | dup | new | record_count |
|---|---|---|---|---|---|---|
| 2026-06-23 05:38 | `c6a88994` | king/tax_delinquent | 0 | 0 | 0 | 0 (failed) |
| 2026-06-23 05:39 | `fa573bfd` | pierce/probate | 122 | 0 | 122 | 122 |
| 2026-07-02 03:55 | `a79865ef` | pierce/probate | 50 | 50 | 0 | 0 |
| 2026-09-08 06:28 | `437ecba1` | pierce/probate | 35 | 0 | 35 | 32 |

- The account is not new. It received 122 leads on 2026-06-23.
- All 50 duplicate hashes on the 2026-07-02 run trace to `delivered_records`
  rows whose `first_job_id` is that account's OWN `fa573bfd` and whose
  `user_id` is the same user. (UI shows 49, not 50, because the API filters
  counts through `actionable_condition()`.)
- Schema-wide: 0 `delivered_records` rows whose `first_job_id` belongs to a
  different user; 0 `results` rows whose job belongs to a different user.
- Quota: the all-duplicate job billed 0. Duplicates do not consume quota.
- Direct URL to another account's job returns HTTP 404 `{"detail":"Job not
  found"}` from the API, not a frontend hide. Verified live in production.

## The real defect

`previous_job_id` in `src/api/routes/jobs.py` picked "newest DONE job on a
sibling config with >=1 visible lead". It had **no bound requiring that job to
precede the job being viewed.**

Reproduced live on the exact reported page: "View previous results" linked to
`/results/437ecba1` (ran 2026-09-08), two months AFTER the run being viewed, and
a run that delivered none of those 49 leads. The run that actually delivered
them is `fa573bfd` (2026-06-23).

So the banner made a true claim, then handed the user a link that disproved it.

Secondary: the table rendered `No records found.` directly under a banner saying
49 records were found and filtered.

## Findings from the Codex review (independently verified)

Round 1 (consult, before implementation):

| # | Finding | Verified at | Severity |
|---|---|---|---|
| 1 | `skip_trace_cache` is tenant-keyed; the `models.py` docstring said address-only and was stale | `skip_trace.py:108` | Low (doc) |
| 2 | `previous_job_id` has no "created before" bound | `jobs.py` + live repro | **High** |
| 3 | Post-crash cleanup releases the quota reservation but NOT dedup claims | `tasks.py:257` | Medium |
| 4 | Plan-cap release deletes a claim whose hash a surviving delivered sibling still needs | `tasks.py:1509` | Medium |
| 5 | `trustee_sale` same-run collapse also sets `is_duplicate`; the banner called those "already received" | `trustee_sale_finalize.py:135` | Medium |
| 6 | `bridgeleads_app` has ALL privileges REVOKED on `delivered_records`, with a hard-fail verifier | `provision_rls_roles.sql:146` | Design blocker |

Round 2 (review gate) - 1 [P1], 4 [P2], all fixed:
- [P1] migration built the index non-concurrently inside the transaction holding
  `ACCESS EXCLUSIVE` on `results`.
- [P2] a finished source job still does not prove delivery.
- [P2] the backfill compared job creation times.
- [P2] partial provenance was stated as a whole-set claim.
- [P2] the header labelled same-run collapses "already delivered".

Round 3 (re-review of those fixes) - 7 [P2], 6 fixed, 1 recorded as a
disagreement. Detail in the Review section below.

Production sizing:
- 0 claims held by non-done jobs today.
- 44,865 of 54,571 claims (82%) are dangling: `first_job_id` points at a deleted
  jobs row and `first_result_id` is NULL. Ledger attribution is unavailable for
  most history, which is why the fix must not promise per-run provenance.
- 33,522 duplicate-flagged rows have no surviving claim, but they are 1 user /
  2 jobs, all on 2026-09-04. Not systemic.
- 0 same-run collapse rows in production today (finding 5 is latent).
- 11 jobs schema-wide currently render an all-duplicate page.

## Plan (owner chose: all three phases, name the earlier run's date)

### Phase 1 - BE
- [x] `jobs.py`: bound `previous_job_id` to jobs created before the viewed job
- [x] `previous_job_run_at` returned so the copy can name it
- [x] Migration 089: `duplicate_source_job_id` / `_at` / `duplicate_reason` on
      `results`, stamped at classification time
- [x] `duplicate_sources` / `unattributed_duplicate_count` /
      `same_run_duplicate_count` on `ResultsPage`
- [x] `models.py`: correct the stale `SkipTraceCache` docstring

### Phase 2 - FE
- [x] Banner names the earlier run's date, but only when it can prove it
- [x] Empty state: "No new leads in this run." instead of "No records found."
- [x] Zero em dashes in modified user-facing copy

### Phase 3 - worker dedupe semantics
- [x] Release dedup claims in the post-crash failure path
- [x] Plan-cap release: keep a claim a surviving deliverable sibling needs
- [x] `same_run` collapse distinguished from prior delivery

## Review

### Why the account saw this
Not cross-account. The account was created 2026-06-23 and a Pierce/probate run
that same day delivered it 122 leads. The 2026-07-02 re-run covered an
overlapping window; all 50 hashes trace to that account's own `fa573bfd`.

### Design change forced by review
The first design had the results API join `delivered_records` to name the true
source run. That is impossible: `bridgeleads_app` has ALL privileges REVOKED on
that table and `provision_rls_roles.sql` hard-fails if it ever holds one. The
ledger could not answer honestly anyway - it is a CLAIM ledger written before a
job finishes, it is mutated by three release paths, and 82% of its production
rows already point at a purged job. So provenance is stamped onto the result row
when it is classified, where it is immutable and readable from the request path.

### Round 3 findings (6 fixed)
- autogenerate would still propose a plain `op.create_index` for the manually
  managed index. `alembic/env.py` now excludes it via `include_object`.
- the index script documented `$DATABASE_URL_SYNC`, which is the worker's
  non-owner role and a SQLAlchemy URL psql cannot parse. Now documents an owner
  session connection, matching `create_result_fingerprint_index.sql`.
- the named-run sentence opens "All {total_scraped}", which counts same-run
  collapses too, so 5 prior-run duplicates plus 2 collapses read "All 7 picked
  up by your run". It now requires zero collapses.
- the `since_last_run` branch ran before the same-run branch and swallowed it.
- the backfill's `first_delivered_at` guard used `NOW()`, which is transaction
  START time, so a stalled older transaction could still acquire a released
  claim afterwards and pass. Now also requires
  `source.finished_at < own.created_at`.
- the four count queries were separate READ COMMITTED snapshots; a finalize
  committing between them could make same_run exceed duplicate_count and render
  a negative. One aggregate now.
- **the strongest finding**: the cap-release tests held their own copy of the
  production SQL, so deleting the guard in `tasks.py` left all five green. The
  statement moved to `tasks_helpers/dedup.release_capped_dedup_claims` and the
  tests call it. Verified by deleting the guard from production code: the test
  now fails, and it did not before.

### Disagreement recorded, not silently resolved
Codex round 3 also flagged the header label "already delivered" for a claim
whose source run failed. It is technically right that a claim is not proof of
delivery. But the owner's standing rule (2026-09-04) defines a duplicate as
"already delivered and paid for on an earlier run", and CLAUDE.md gives the doc
priority where it speaks. The narrow claim (naming a specific run) is guarded;
the category label follows the product's own definition.

### One Codex claim that was wrong
Round 1 said the API had a per-group N+1 for source-job availability. It did
when Codex first looked; it had already been batched into one `IN` query before
round 2, which round 2 confirmed. Kept as written.

### Checks
- `ruff check src/ tests/ scripts/ alembic/` - passed
- `tsc --noEmit` (frontend) - passed
- `eslint .` (frontend) - passed
- `scripts/export_openapi.py --check` - up to date (70 insertions, 0 deletions)
- `openapi-typescript` regen - 45 insertions, 0 deletions
- full migration chain 001 -> 089 applied clean on a fresh database
- 17 new backend tests. They test the fix, not the code: removing the
  `created_at` bound fails 2 of them, and removing the plan-cap guard from
  production code fails another.
- affected surfaces (`test_duplicate_provenance`, `test_results_new_count`,
  `test_tax_cap_jobs`, `test_jobs`, `test_segments_tax_cap`,
  `test_scheduled_job_dedup`, `test_batches_read`) - 89 passed, and three
  consecutive clean runs of the first six

### Suite note
An early full run showed failures that moved around between runs. They were
environmental, and proven so: with Redis flushed on an identical rig, this
branch ran the suspect files at 3 failed / 57 passed while a clean `origin/main`
worktree ran the same files at 23 failed / 37 passed. Main was worse. The one
genuinely flaky thing that WAS mine - test helpers opening their own session
instead of the committed fixture session - is fixed, and those tests now pass
repeatedly.

### Live verification
Production cannot serve the corrected page (nothing deployed, and the new fields
do not exist there), so the fix was driven in a real browser against a local API
plus an isolated database seeded with the incident's exact shape: an earlier run
that delivered leads, the all-duplicate run being viewed, and a LATER run.

Verified in Chromium, all passing:
- banner reads "All 49 records from Mar 26, 2026 to Jun 24, 2026 were already
  picked up by your run on Jun 23, 2026"
- "View previous results" links to the JUNE 23 run, not the later one
- following that link lands on a page listing that run's 6 real leads
- the table reads "No new leads in this run.", not "No records found."
- no em dash anywhere in the rendered page
- the old "duplicates of leads you already received" copy is gone

Rig torn down afterwards: servers stopped, FE `.env.local` deleted, verify DB
dropped.

### Not done
- Nothing is merged or deployed, so the corrected page has not been seen in
  PRODUCTION. The bug was verified live in production; the fix was verified live
  only against the local stack.
- `scripts/backfill_duplicate_provenance.py` has not been run. Until it is, the
  11 existing all-duplicate pages show the unattributed wording rather than a
  named run.
- `scripts/create_result_duplicate_source_index.sql` has not been run. The
  grouping query falls back to the existing `job_id` index until it is.
