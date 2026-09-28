# UX queue 2c: Q1 (F-001) run-count breakdown (migration 106)

Branch `feat/run-count-breakdown-2c` (worktree `C:/Users/Windows/bl-wt/eligibility`), from
`origin/main` `c0b09b7a`; its head `c73a47da` is `c0b09b7a` + the handoff doc only (no code),
so the line numbers below hold for both. Before building: `git fetch`, and if `origin/main`
moved, rebase and re-verify the cited lines (Codex r7 P2). Spec: FE `docs/ux-audit/phase-3.0-contracts.md` "Q1 (F-001)".
Status: **PLAN, not built. Codex PLAN: GO (round 8). Waiting on the owner's confirmation.**

Codex plan log (read-only `codex exec`, prompts in the session scratchpad): r1 CHANGES
(6 P1: attempt scoping, READ COMMITTED, partition, live unclassified, tax cap, deploy fact)
-> r2 CHANGES (per-attempt accounting, fail-open migrate, 2c-bis as release dependency) ->
r3 CHANGES (2c-bis DEPLOYED first, empty run, nullable types, runbook) -> r4 CHANGES
(executable order, T8 for both outcomes, helper location) -> r5 CHANGES (retry_count under
lock, 2c-bis scope) -> r6 CHANGES (explicit W3 branches, tuple contract, log form) -> r7
CHANGES (live breakdown only when done, T8 split by winner, log tests) -> **r8 GO** (no
P1/P2/P3). One r1 finding was answered by argument, not code (r1 P1-2); Codex accepted it
in r2 with the narrower reading recorded in W2.

## Problem (unchanged from the contract)
A finished run shows numbers that disagree: `records_found` 265, the worker log's
"12 new leads, 252 duplicates" (264), the results page's `total_scraped` 258 (= 246 + 12).
Each is counted at a different point over a different row set, and nothing names the rows
in between.

## Facts re-verified in today's code (line numbers as of `c73a47da`)
| Fact | Where |
|---|---|
| `records_found = len(records)` written BEFORE the probate living-TOD filter and BEFORE insert | `src/workers/tasks.py:949-956` |
| living-TOD filter drops rows after that; count known in-process as `_dropped_tod` | `tasks.py:968-984` |
| `_set_status("enriching", record_count=len(records))` (post-TOD) | `tasks.py:1000` |
| insert merges rows sharing `(job_id, source_fingerprint)` via `on_conflict_do_nothing`, uncounted | `tasks.py:1047-1065`, `1151-1162` |
| log "N records saved (U new leads, D duplicates)": U = claimed hashes, D = every row with a hash that lost its claim, address or not | `tasks.py:1265-1312` |
| `dup_count += collapse_same_run_siblings(...)`; `dup_count -= transfer_undelivered_claims(...)` | `tasks.py:1384-1395`, `1666-1682` |
| plan cap marks `enrichment_data.delivery_excluded_reason='over_quota'` on non-dup address-actionable rows, AND on same-run siblings of capped rows | `tasks.py:1734-1926`, `plan_cap.py:56-87` |
| `billable_count` = non-dup AND `actionable_sql` (address AND not over_quota), read in the transaction the done-CAS commits | `tasks.py:2220-2227` |
| billing CAS `billing_applied_at IS NULL`; else branch reuses `job.billed_count` | `tasks.py:2249-2254`, `2384-2394` |
| done-CAS `_set_status(... "done", record_count=display_count, commit=False)`, then ONE `db.commit()` with billing | `tasks.py:2406-2432` |
| final log "Job complete: N new leads (D duplicates filtered)" | `tasks.py:2439` |
| `superseded` is written ONLY onto an OLDER run's rows, by a LATER run's claim transfer; the holder must be `done` (or failed/cancelled unbilled). "A run still in flight is never robbed." | `src/workers/tasks_helpers/dedup.py:847-1057` |
| API aggregate: actionable rows, not `superseded`, LIVE; `already_delivered` also applies `tax_cap_condition(today)` | `src/api/routes/jobs.py:646-701` |
| `already_delivered` = `is_duplicate AND coalesce(duplicate_reason,'prior_run')='prior_run'` | `src/api/results_category.py:34-48` |
| DONE label "Complete: {record_count} records" | `src/api/schemas.py:1402-1407` (test `tests/test_job_progress_observations.py:326`) |
| `ResultsPage.total_scraped` doc "all records before dedup" is wrong | `schemas.py:1847` |
| watchdog / transient retry reset `records_found=NULL` (so it is per attempt) | `status.py:788`, `scheduler_helpers/health.py:324` |
| post-done backfill selects `j.status='done'` and can make rows actionable later | `src/workers/mailing_recovery.py:167,187,563,581` |
| migration pattern for nullable `jobs` columns: `SET LOCAL lock_timeout='5s'` + `add_column` | `alembic/versions/099_job_progress_observations.py` |
| `jobs` grants are table-level (new columns inherit) | `scripts/provision_rls_roles.sql:87` |

## Production measurement (read-only, counts only, 2026-09-28, `bl-checks/breakdown_2c.py`)
Done jobs finished in the last 60 days: **101**. Buckets computed with the proposed precedence.
- Audit job **0e11ee17**: found 265 = persisted 265 = **7 no_address + 0 same_run + 246
  already_delivered + 0 over_quota + 12 new**. Zero fingerprint merges. The contract sums
  exactly. (The worker's 252 = 246 + the 6 addressless duplicates.)
- Audit job **89b92687**: found 125 = persisted 125; the audit saw 123 already delivered, today
  it is 125 with 0 no_address. **Live drift confirmed**: 2 rows gained an address after done.
- `records_found` is NULL on **92/101** (column added by 099; older jobs). Of the 9 with it:
  **persisted == found on all 9**: fingerprint merges and living-TOD drops measured **0** across
  2,525 records. Persisted > found: 0.
- Live `new` != `record_count` on **13** jobs (backfills, and one legacy job with record_count
  10344 / billed 0). This is why a snapshot is needed.
- `superseded`: 2 jobs, **all addressless** (none has an address).
- over_quota rows: 1 job (b2f2ecd5, 16,007 rows). same_run: 3 jobs (e.g. 37014cb9: 3 + 28 +
  1302 + 423 = 1756, exact).

## Contract (proposed; owner has not approved it yet)
```
records_found = dropped_before_save + no_address + same_run_merged
              + already_delivered + over_quota + new           (new = record_count = billed)
```
Per persisted row of the job, first match wins:
1. `no_address`: NOT `address_actionable_sql` (any duplicate state).
2. `same_run_merged`: `is_duplicate AND duplicate_reason='same_run'` (incl. siblings the cap
   marked over quota: they are combined into a property that was capped).
3. `already_delivered`: `already_delivered_sql` (prior_run or NULL reason).
4. `over_quota`: NOT duplicate AND over-quota mark.
5. `new`: NOT duplicate AND not over quota (= `actionable_sql AND is_duplicate=false`,
   exactly the billing predicate).
6. anything else (`superseded` WITH an address): `unclassified`. Impossible at snapshot time
   (above), so it is an assertion, not a bucket; see step W3.
`dropped_before_save = records_found - persisted` (living-TOD drops + fingerprint merges).

Deliberately NOT applied to the snapshot: `tax_cap_condition(today)`. It is a date-relative
VIEW filter (billing does not apply it either), so it cannot be frozen. The results tab keeps
applying it to its live count.

## Design
### Storage: migration 106, six nullable integer columns on `jobs`
`breakdown_dropped_before_save, breakdown_no_address, breakdown_same_run_merged,
breakdown_already_delivered, breakdown_over_quota, breakdown_new` + nothing else: the
snapshot's presence IS `breakdown_new IS NOT NULL` (all six are written by one statement).
Why columns over one JSON column: typed, and 2d (`already_delivered` on the jobs list) reads a
plain int off the row. No CHECK constraints (follows 099: a constraint on a hot table is a
second migration to change). No backfill: old jobs stay NULL and are reported `live`.
Lock safety exactly as 099: `SET LOCAL lock_timeout = '5s'`, catalog-only nullable ADD COLUMN.

### Worker (tasks.py + one new helper module)
- W1. New `src/api/run_breakdown.py` (Codex r4 P2: one concrete shared location). It holds
  only the raw-SQL builder and pure functions (partition normalization, the W3 guard,
  `breakdown_from_job` validation), no FastAPI or worker imports, so the sync worker and the
  async API run the identical statement. Same placement as `src/api/lead_actionability.py`
  and `src/api/results_category.py`, which the worker already imports. ONE aggregate statement over `results` for (job_id, user_id)
  returning persisted + the five row buckets + unclassified, built from the existing
  predicates (`address_actionable_sql`, `actionable_sql`, `already_delivered_sql`, the
  over-quota key). No copy of any predicate.
  The statement is a PARTITION, not independent FILTERs (Codex r1 P1-3):
  `SELECT CASE <precedence> END AS bucket, count(*) ... GROUP BY 1`, so a row lands in
  exactly one bucket by construction; `unclassified` is the CASE's ELSE. A job with zero
  persisted rows returns NO groups: the helper normalizes missing buckets to 0, so an empty
  run bills 0 and snapshots six zeros (Codex r3 P2; tested).
- W2. Replace the `billable_count` SELECT (`tasks.py:2220-2227`) with that statement:
  `billable_count = breakdown.new`. One statement = one MVCC snapshot for every bucket, and
  the same Python value feeds `billed_count`, `record_count` and the snapshot, so those three
  cannot disagree. (Codex r1 P1-2 asked whether READ COMMITTED lets rows change between the
  SELECT and the billing UPDATE. Nothing else writes THIS job's rows during finalization:
  mailing recovery selects `status='done'` only, claim transfer never robs an in-flight run,
  skip trace does not touch address/duplicate/over-quota. The one exception is a stale
  attempt of the same job, which is P1-1 below and is fenced there.)
- W3. Write the six columns in the done-CAS `_set_status(..., "done", ...)` call (add the
  columns to `JobUpdateFields` typed `int | None`, since the refused path writes NULL;
  Codex r3 P2), only when ALL hold, checked in Python before the UPDATE:
  - (a) the billing CAS fired this attempt (`billed_now`);
  - (b) ownership: right after the billing CAS (which holds the `jobs` row lock), read
    `started_at, records_found, retry_count` of the row in ONE statement and pass those DB
    values to the guard, never the ORM object's (Codex r5 P1); `started_at ==
    attempt_started_at` (Codex r1 P1-1). This check only decides the SNAPSHOT; stopping a
    stale attempt from billing / completing is 2c-bis's job (see "Pre-existing" below);
  - (c) `records_found` is not NULL and `>= persisted`, AND `retry_count == 0` (Codex r2
    P1-1: `records_found` is per attempt but `persisted` is per job, and an earlier attempt's
    rows survive the idempotent insert, so after ANY retry `records_found - persisted` is not
    this attempt's drop count even when it is non-negative. `retry_count` is the value read
    under the lock in (b). A time-based "rows created
    before this attempt" test was rejected: `started_at` is the worker's clock
    (`status.py` `claimed_at = _now()`), `results.created_at` is the DB's `now()`. A retried
    job reports `live`, which is honest);
  - (d) `unclassified == 0`;
  - (e) `persisted == no_address + same_run + already_delivered + over_quota + new` and
    `records_found == dropped_before_save + persisted`, every value `>= 0` (Codex r1 P1-3).
  The four branches, exhaustive (Codex r6 P1):
  1. `billed_now = false` (billed by an earlier attempt): the done-CAS does NOT name the six
     columns: an existing snapshot is preserved, none is created; report `billed_count`.
  2. `billed_now = true`, ownership lost (b fails): the six columns are NOT named either.
     On 2c alone the rest of finalization is today's code; after the rebase onto 2c-bis,
     2c-bis's fence rolls back and stops before this point without releasing anything.
  3. `billed_now = true`, owner, any of (c)-(e) fails: the six columns are written NULL
     (-> `live`) and a WARNING names the failed check.
  4. `billed_now = true`, owner, (c)-(e) hold: the six values are written.
  The snapshot never fails or delays the job by itself. Rationale:
  - `billed_now` false: the job was billed by an earlier attempt and the watchdog can re-run a
    billed non-terminal job (Codex r1 P2-9: NOT legacy-only). That path keeps reporting
    `job.billed_count` exactly as today, never creates a snapshot and never overwrites one.
  - `persisted > records_found`: a re-run whose source set changed keeps rows from the earlier
    attempt (idempotent insert). Measured 0 in prod.
- W4. Final log line from the snapshot: "Job complete: 12 new leads (246 already delivered,
  7 without an address)". Exact form (Codex r6 P2): "Job complete: {new} new lead(s)" then,
  in parentheses, comma-separated, only the NON-ZERO buckets in this fixed order:
  "{already_delivered} already delivered", "{same_run_merged} combined in this run",
  "{no_address} without an address", "{over_quota} over your plan limit",
  "{dropped_before_save} not saved". No parentheses when all are zero. Tested for all-zero,
  one, and all-five cases. With no snapshot: "Job complete: N
  new leads" only, never a duplicate figure (Codex r1 P2-8). The mid-run line at
  `tasks.py:1310` stops claiming "new leads" (its U is claimed hashes, not billed leads, which
  is the 264 in the audit): becomes "{n} records scraped. Checking which are new..."
  (`len(records)` is not the saved count: the insert can merge rows; Codex r4 P2).
  `dup_count` and `unique_count` then have no reader and are removed with their updates (the
  collapse and claim-transfer calls stay; only the counters go).
- Notifications / email / webhook keep `record_count` (unchanged).

### API
- A1. `schemas.py`: new `RunBreakdown` model (six ints; `dropped_before_save: int | None`
  because a live breakdown for a job without `records_found` cannot know it) and
  `breakdown_basis: Literal["snapshot", "live"] | None`.
- A2. `JobResponse` gains `breakdown` + `breakdown_basis`, **snapshot only** (read off the
  row, no query). No snapshot -> both `None`. The list endpoint must not run a per-job
  aggregate (that is 2d's decision). Mapping is explicit, not inferred (Codex r1 P2-10): ONE
  pure function `breakdown_from_job(job) -> tuple[RunBreakdown | None, str | None]`
  (breakdown, rejection reason; the route logs the WARNING when the reason is set; Codex r6
  P2) used by every path that builds a
  `JobResponse` (`_job_response`, the create path `jobs.py:360`, the detail path); all six
  non-NULL AND valid -> snapshot; all NULL -> None; any partial NULL, negative value,
  `sum(six) != records_found`, or `breakdown_new != record_count` or `!= billed_count`
  (Codex r3 P2) -> None + a WARNING log (cannot happen by construction; never
  rendered; Codex r2 P2-6).
- A3. `ResultsPage` gains `breakdown` + `breakdown_basis`: snapshot when present, else the
  LIVE classification from the SAME SQL builder as W1 (one extra aggregate on the request's
  RLS session, `job_id` AND `user_id` in every branch) with `dropped_before_save =
  records_found - persisted`. A live breakdown is computed ONLY for `status = 'done'`; any
  other status returns `breakdown = None, breakdown_basis = None`: before done,
  `records_found` is written before the TOD filter and the inserts, so pending rows would
  read as "not saved" (Codex r7 P1). For a done job with `retry_count > 0`,
  `dropped_before_save = None` (attempt-scoped `records_found` vs cumulative rows; Codex r7
  P1). Otherwise three live cases, each tested (Codex r2 P2-5):
  `records_found` NULL (92/101 recent prod jobs) -> breakdown with `dropped_before_save =
  None`, documented as "unknown" (the five row buckets still partition the persisted rows);
  `records_found < persisted` -> `breakdown = None`, `breakdown_basis = None` (unavailable,
  it cannot reconcile); otherwise the full six. If the live partition has `unclassified > 0` (a superseded row that later gained an
  address) -> `breakdown = None`, `breakdown_basis = None` rather than a breakdown that does
  not sum (Codex r1 P1-4).
  **The breakdown is RAW, never view-filtered** (Codex r1 P1-5): no tax cap, no view
  filters, snapshot or live. It partitions the job's persisted rows. The existing live
  counts (`total_scraped`, `duplicate_count`, `new_count`, `already_delivered_count`, ...)
  are unchanged and stay what the tabs show; `already_delivered_count` keeps the tax cap, so
  on a tax_delinquent run it can be smaller than `breakdown.already_delivered`. Both
  docstrings say so, and T6 pins the boundary (a tax row past the cap counts in the
  breakdown, not in the tab).
- A4. DONE label -> "Complete: N new leads" ("1 new lead"). Update the one test asserting the
  old text (`test_job_progress_observations.py:326`).
- A5. Fix `ResultsPage.total_scraped` doc: "actionable rows, after the save-time merge,
  excluding superseded, live (duplicates included)". Name kept (FE reads it).
- A6. Regenerate `schema/openapi.json` (`scripts/export_openapi.py`, then `--check`) and a
  structural diff vs `origin/main`. Additive only.

### FE (separate follow-up PR after the BE is live; NOT in this PR)
Regenerate types (the drift gate turns every FE PR red once BE main changes, so this follows
immediately), render the breakdown on the run and results pages.

## Tests (real DB `bridgeleads_eligibility_test`, each proven RED on unfixed code)
- T1 `tests/test_run_breakdown.py`: a job with rows in every state (addressless dup,
  addressless non-dup, placeholder address, same_run, same_run+over_quota mark, prior_run,
  NULL-reason dup, over_quota non-dup, new, superseded without address) -> exact bucket
  counts; buckets sum to persisted; `new` equals the billing predicate count.
- T2 precedence: addressless over_quota-marked row -> `no_address`; same_run over_quota ->
  `same_run_merged`.
- T3 superseded WITH address -> `unclassified == 1`; the snapshot guard writes NULLs; the
  live ResultsPage returns `breakdown = None`.
- T4 user scoping: another user's rows on the same job id never count (helper level).
- T5 snapshot decision (the W3 guard as a pure function over the partition + job fields, so
  every exit is tested on real rows). Two distinct rules (Codex r2 P2-7):
  `billed_now = false` -> the done-CAS does not touch the six columns at all (an existing
  snapshot survives; none is created) and the reported count is `billed_count`, tested with
  persisted rows changed after the first bill (Codex r1 P2-9); ownership lost -> the six
  columns are not named (an existing value survives); owner with any of (c)-(e) failing ->
  all six written NULL. Happy path writes six values that sum. One test per W3 branch. The
  retry case of r2 P1-1 is tested explicitly: `retry_count = 1`, earlier-attempt rows
  present, `persisted <= records_found` -> NULLs.
- T8 real interleaving, two DB sessions (Codex r2 P2-4, r4 P1): attempt A (stale token)
  reaches finalization after attempt B re-claimed the row (new `started_at` committed). On
  2c's code alone: A's ownership read under the billing row lock sees B's token -> no
  snapshot from A. Split by who holds the row first (Codex r7 P1): (i) B re-claims first ->
  A writes no snapshot, and after the rebase onto 2c-bis A also neither bills, nor marks
  done, nor releases B's reservation or claims; (ii) A takes the billing lock first (B's
  re-claim waits) -> A is still the owner, so A may bill, complete and snapshot, and B's
  re-claim then finds a terminal row and does nothing.
- T9 empty run (Codex r3/r4 P2): zero persisted rows -> partition all zero, billing 0,
  snapshot six zeros that validate (`breakdown_from_job` returns a snapshot, not None).
- T11 final log line (W4; Codex r7 P2): no snapshot -> "Job complete: N new leads" only;
  all-zero buckets -> no parentheses; singular "1 new lead"; one non-zero bucket; all five
  non-zero in the fixed order; zero buckets omitted. A pure formatter, tested directly.
- T6b live breakdown gating (Codex r7 P1): non-done status -> None/None; done with
  `retry_count > 0` -> `dropped_before_save` None, the five row buckets present.
- T10 `breakdown_from_job` rejects when any of `records_found`, `record_count`,
  `billed_count` is NULL, before any arithmetic (Codex r4 P2). It stays pure: it returns
  `(breakdown | None, reason | None)` and the API route logs the WARNING (Codex r5 P2).
- T5b `tasks.py` wiring: `billable_count` comes from the helper and the done-CAS passes the
  guard's columns (source-inspection test in the style of
  `test_run_scrape_job_still_starts_the_heartbeat`), plus mutation checks (break the
  partition CASE -> T1 red; drop the ownership check -> T5 red).
- T6 API through the real HTTP client + `get_rls_db` tenant session (Codex r1 P2-11):
  list, detail and results endpoints with and without a snapshot; partial-NULL row -> None;
  live `dropped_before_save` None when `records_found` NULL; another tenant's job -> 404 and
  its rows never counted; tax-cap boundary (P1-5); DONE label text.
- T7 migration 106 up/down on the test DB: the six columns exist, `data_type = 'integer'`,
  `is_nullable = 'YES'`; gone after downgrade (Codex r5 P2).
Then full suite in 8 parts (background, one at a time, real exit codes), ruff.

## Steps
- [ ] 1. Codex PLAN review until PLAN: GO. **Then STOP for the owner's confirmation to
  build** (the owner's standing workflow). The two owner decisions below (schema gate vs
  runbook; 2c-bis order) gate MERGE/DEPLOY, not building.
- [ ] 2. Migration 106 + model columns + `JobUpdateFields`; apply to test DB.
- [ ] 3. W1 helper + T1-T4 (RED first: the module does not exist / wrong counts).
- [ ] 4. W2-W4 in tasks.py + T5 (+ mutation check).
- [ ] 5. A1-A5 + T6; OpenAPI regen + check.
- [ ] 6. Full suite (8 parts) + ruff; security review x2 clean: the Master Security Review,
  `docs/security/SECURITY_PROMPT_PACK.md` §14, translated per `.claude/rules/security.md`
  (for 2c: every new query filters `user_id` AND `job_id` and runs on the RLS session in the
  API; no raw input spliced into SQL (the builder splices only code constants); errors carry
  no DB detail; no new secret/env; OpenAPI additive only; the migration's lock behavior).
- [ ] 7. Codex diff review on `origin/main...HEAD` until GATE: PASS. **Do not merge.**
- [ ] 8. 2c-bis (owner-approved order; its own plan, Codex, tests, gate): build, merge,
  DEPLOY; confirm its SHA SUCCESS on api/worker/beat, old containers gone, quiet.py all zeros.
- [ ] 9. Rebase 2c onto the post-2c-bis `origin/main`; re-run T1-T10 (T8 in its rebased
  form) + the full suite; Codex diff review again on `origin/main...HEAD` to GATE: PASS
  (a review that predates the rebase does not cover it).
- [ ] 10. Deploy 2c per "Migration / deploy plan" (intake paused, quiesced, merge
  `--match-head-commit`, verify objects via api/worker/beat, SHA, health, logs, resume
  intake, smoke test on an eligible job).
- [ ] 11. FE follow-up PR (types regen + render).
- [ ] 12. BUILD_JOURNAL entry.

## Migration / deploy plan
- 106 = six nullable int columns on `jobs`, catalog-only, `lock_timeout 5s`.
- Merge IS deploy. Corrected fact (Codex r1 P1-6, verified in `start.sh`): EVERY service
  (api, worker, beat) runs `scripts/migrate.py` (advisory-locked, idempotent) before it
  starts; the api fails CLOSED on a migration error, worker and beat fail OPEN. So on a
  normal deploy each new process starts only after 106 is applied. The residual case is a
  worker whose migrate fails: new code then names the columns in every `select(Job)` and
  fails loudly until the api applies 106, which is the same accepted model as every column
  migration since 099.
  **Codex r2 P1-2 disagrees and calls this a release blocker.** `start.sh:17-55` documents
  the fail-open as a deliberate trade-off (a worker whose migrate env differs must not
  "never start"). Changing it is a start.sh/deploy-policy change for every future
  migration, not 2c. What 2c's deploy does instead: (1) quiesced merge (nothing to fail);
  (2) the api fails CLOSED, so "api healthy on the new SHA" proves `migrate.py` reached head;
  (3) verify the six columns by the objects before any run is triggered. **Owner decides**:
  accept the runbook, or add a schema gate (worker refuses to consume until
  `alembic_version`/objects match head) as a separate item first.
- Before merging: quiet.py all zeros, no non-terminal jobs, no batch running, no long
  `results`/`jobs` scans or backfills (`incident_backfill_blocks_migration`: the ADD COLUMN
  needs a brief ACCESS EXCLUSIVE on `jobs`; `lock_timeout 5s` makes it fail fast rather than
  queue every job read behind it), `main` unchanged since the review.
- Intake pause (Codex r3 P2). There is NO scrape-dispatch kill switch in settings, so the
  pause is one of: (A, preferred) the owner scales the Railway `worker` service to 0 before
  the merge and back to 1 after step "After" passes (a worker migrates on boot too, so this
  loses nothing); or (B) merge in a window where no scheduled config is due for 30 min
  (read-only check of next-due configs) with quiet.py all zeros, AND no manual run is
  started by anyone (owner included) until "After" passes: (B) pauses scheduled intake
  only (Codex r4 P2). Owner picks.
- After: verify BY THE OBJECTS through EACH service's own connection (`railway run
  --service api`, `--service worker`, `--service beat`: the six columns in
  `information_schema.columns`, type integer, nullable YES; read-only), deployed SHA on
  api/worker/beat, `/health`, logs; only then resume intake. Smoke test on the first
  ELIGIBLE job (done after the deploy, `retry_count = 0`, billed by that attempt): its
  snapshot is present, sums to `records_found`, and `breakdown_new = record_count =
  billed_count`. A job that is not eligible has no snapshot: `JobResponse.breakdown` is None
  and its results page reports `breakdown_basis = "live"` (Codex r4 P2).
- 106 is **forward-only in production** (Codex r1 P2-7): a code revert is the rollback
  (old code ignores the columns). `downgrade()` exists for the test DB and would drop the
  snapshots; never run it in prod while new code is deployed.

## Pre-existing P1 found in review, NOT fixed by 2c (owner decision needed)
Codex r1 P1-1: finalization is not attempt-scoped. The watchdog re-queue sets
`started_at = NULL` and `status = pending` (`scheduler_helpers/health.py:300-330`); a stalled
(not dead) old attempt that later resumes reaches the billing CAS (`tasks.py:2250`, scoped by
`billing_applied_at IS NULL` only) and the done-CAS (`tasks.py:2407`, no
`expected_started_at`), so it can bill its view and mark the REPLACEMENT attempt's row done.
2c fences only its own write (W3 check (b)): a stale attempt never writes a snapshot.
Fixing the CASes themselves is a money-path change with its own design problem: the
done-CAS failure branch today releases the quota reservation and the dedup claims
(`tasks.py:2414-2431`), which is right for "cancelled" and WRONG for "lost ownership" (the
replacement owns them). Proposal: its own queue item (2c-bis) with its own plan + Codex, rather
than widening a migration PR. **Codex r2 P1-3: acceptable ONLY as a hard release
dependency**: 2c does not make the defect worse and does not rely on it (W3 (b) keeps a
stale attempt from writing a snapshot), but features must not ship on top of a money path
known to be unfenced. Required scope of 2c-bis (Codex r5 P1; its own plan will detail it):
fence the billing CAS AND the done-CAS with the attempt token (`started_at = attempt`);
when ownership is lost, roll back and stop WITHOUT releasing the reservation or the dedup
claims (the replacement owns them), which means a separate branch from today's "externally
terminalized" path (`tasks.py:2414-2431`); the `billed_now = false` path must also check
ownership before it proceeds to the done-CAS; tests for both race outcomes (the stale
attempt reaches the lock first or second).
So the proposed ORDER is: build 2c now, build 2c-bis, merge 2c-bis
first (no migration), and only merge 2c once 2c-bis is DEPLOYED, not merely merged (Codex
r3 P1): 2c-bis SHA SUCCESS on api/worker/beat, old worker containers gone (Railway shows one
deployment per service), quiet.py all zeros so no attempt started on old code is still in
flight. This order is a REQUIREMENT of this plan, not an option (Codex r6 P1): 2c does not
proceed to merge until 2c-bis is independently merged, deployed, SHA-verified and quiet.
**Owner: confirm.**

## Open questions for the owner
The build proceeds on the PROPOSAL of each question below as an assumption, and only after
the owner has explicitly approved (or changed) it at the step-1 stop (Codex r6 P2).
1. **Headline after a backfill: snapshot or live?** Proposal: the snapshot is the headline
   (it is what was billed and delivered at completion); live counts stay in the tab badges.
   89b92687 already shows why (123 -> 125 since the audit).
2. **`superseded` bucket?** Proposal: NO. Superseded is only ever written onto an older run
   AFTER it finished, so it can never appear in a snapshot; in prod all superseded rows are
   addressless (they fall in `no_address` live). The snapshot asserts it (T3).
3. **Split `dropped_before_save` by reason (living-TOD vs merged)?** Proposal: NO for now.
   Measured 0 of 2,525 records on the 9 jobs that carry `records_found`; the TOD exclusion
   is already named in the run log. Revisit if a real run shows a non-zero value.
4. **Customer wording** (FE PR, noted here so the BE names fit): "Not saved", "No address",
   "Combined in this run", "Already delivered", "Over your plan limit", "New leads".

## Review
(filled in after the build)
