# Audit #4: security review of the changes since 786efcf0 (Claude)

Scope: the diff `786efcf0..ee601b55` (2515 lines), plus frontend commits `c7d78fb` and `d4ba581` in bridgeleads-web.
Method: I read the diff and the full surrounding functions: the dispatcher tick lines 130-636, the helpers that commit or lock, `get_auth_context`, `download_export`, `enqueue_scrape_job`, `dispatch_batch_run`, the `run_scrape_job` preamble, `plan_reconciliation`, and the rate-limit fallback.
Constraints: read-only. I ran no pytest, opened no DB connection and contacted no production service. Findings from audit #3 (S3-01..S3-55) are not repeated unless this delta changes them.

**Result: no P0, no P1, no P2 in the delta.** There are two P3 items: one incomplete data fix and one gap in how far S3-09's rate-limit coverage reaches.

## Findings

| id | sev | category | file:line | evidence | exploit prerequisites | impact | remediation | regression test needed | status |
|---|---|---|---|---|---|---|---|---|---|
| D4-1 | P3 | Billing / data integrity (F-043 fix incomplete) | src/api/routes/scrapers.py:476-481 (delete now clears `paused_reason`); src/api/entitlements.py:547 (revive keys on `not active AND paused_reason='entitlement'`); src/api/entitlements.py:411-425 (`current_batch_child_clause`) | The fix only applies to deletes from now on. Scrapers deleted while paused by a downgrade, before this change, still have `active=False, paused_reason='entitlement'`, and no migration or backfill touches them. In the data they look exactly like a real downgrade pause. On the next upgrade (with `ENTITLEMENT_ENFORCEMENT` on), `plan_reconciliation` puts them in `revive_ids`, turns them back on, and they are scraped and billed. Batch detail and batch lists also count them as children now, because `current_batch_child_clause` includes paused rows. | A user deleted a scraper while it was downgrade-paused, before this deploy, and later upgrades. No attacker is involved. | A scraper the user deleted comes back to life, runs and bills. This is the bug F-043 was meant to close, and it stays open for older rows. Blast radius is limited to the owner's own account. | One-off backfill: for each config with `active=False AND paused_reason='entitlement'` that has a `scraper_deleted` audit event after its last pause, set `paused_reason=NULL`. Longer term, add an explicit `deleted_at` so a delete never looks like a pause again. | A config with a `scraper_deleted` event and `paused_reason='entitlement'` is not revived by `apply_reconciliation_async` and is not counted in `get_batch` or `list_batches` | SUSPECTED (depends on production data; not queried) |
| D4-2 | P3 | Rate limiting (S3-09 coverage) | src/api/routes/batches.py:714 (`download_batch`), :844 (run download); src/api/routes/segments.py:807, :835 (`/intersection/export`, `/union/export`) | The new `export` zone (20/min, still limited in-process if Redis is down) covers only `/jobs/{id}/download` and `/export-url`. The batch combined-CSV downloads and the segment exports rebuild a CSV from the DB, decrypting PII each time, just like the job download. They are still in `general`: 60/min, and no limit at all while Redis is down. | An authenticated account | The same CPU and PII-decrypt amplification S3-09 describes, through routes the fix did not reach. Three times the budget normally, unbounded during a Redis outage. | Move those four routes to `zone="export"` (or a sibling zone listed in `_FALLBACK_ZONES`) | 21st batch or segment export in 60 s returns 429; still 429 with Redis unreachable | CONFIRMED (by reading the code) |

Accepted residual, not a finding. The download-token path (`_user_from_download_token`, jobs.py:1329) checks the token's jti and the owner's logout-all, but not the session family. An `/export-url` token minted just before a single-session logout stays usable for its 60 s lifetime. An emailed 48 h link survives a single-session logout; it dies only on logout-all. This is by design: download tokens are job-bound and short-lived. S3-07 asked only that session JWTs be refused in `?token=`, and they now are.

## Checked and clean

- **alembic/env.py** — The only change adds `ix_pending_skip_trace_queued_frontier` to `CONCURRENT_INDEXES`, so autogenerate will not propose a blocking build. No security effect.
- **alembic/versions/103_pending_skip_trace_queued_frontier.py**
  - It adds only a partial btree index: no table, no column, nothing that needs RLS or grants.
  - Every f-string SQL interpolates constants only (`_INDEX`). The catalog lookups bind `:n`.
  - It runs in autocommit with `lock_timeout 5s`.
  - It never drops a same-named index on another table (it aborts instead), and it never drops a constraint-backing index. Dropping an invalid index is safe because `scripts/migrate.py` serializes migrations with an advisory lock.
  - The index is not a security control, so a missing index can only slow things down; nothing fails open.
- **alembic/versions/104_jobs_one_active_per_config.py**
  - A partial UNIQUE index on `jobs(scraper_config_id)` where status is active. No RLS or grant surface.
  - The duplicate pre-check aborts rather than cancelling anything. SQL uses constants only, and the table name is fully qualified (`public.jobs`).
  - A wrong-shaped or invalid index is dropped and rebuilt. If the rebuild fails, the API pre-check (`_run_in_flight`) still applies, so the guard degrades to the application layer rather than disappearing.
  - The watchdog retry (`tasks_helpers/status.py:785`) only moves an already-active row back to `pending`, so it cannot collide with the index.
- **src/api/entitlements.py**
  - `current_batch_child_clause()` is a pure `or_(active, paused_reason='entitlement')`. It is always ANDed with the existing `user_id` filter at all three call sites (batches.py:585, :670; batch_tasks.py:175).
  - The legacy-row caveat is D4-1.
- **src/api/middleware/rate_limit.py**
  - Adds zones `export (20/60)` and `writes (30/60)`, both in `_FALLBACK_ZONES`, so they keep an in-process limit during a Redis outage instead of failing open.
  - Both use 60 s windows, so the fallback's shared-cutoff eviction stays sound. The fallback key includes the zone, so zones cannot collide.
  - Keys are user ids, which a client cannot choose.
- **src/api/quota.py**
  - `run_eligibility` gives exactly the same verdict as the old `quota_block_reason`: frozen first, then ended, then over limit, otherwise allowed. `quota_block_reason` still returns the message or None, so all five enqueue gates behave as before: POST /jobs (jobs.py:317), POST /batches (batches.py:276), the batch fan-out (batch_tasks.py:149), and the scheduler (dispatch.py:129, :385).
  - `now` is normalized once. The `reset is None` branch is only reachable when `entitlement_ends_at` is set, because frozen accounts return earlier.
- **src/api/routes/batches.py** — The two queries that gained the clause keep `ScraperConfig.user_id == current_user.id`. No new response fields.
- **src/api/routes/billing.py** — `usage_view` reads only the caller's own `User`, and the route is still rate-limited in `general`. `run_eligibility` is for display only; enforcement stays server-side. `plan` shows `pending_plan` after a boundary, and this value is never used for authorization. `response_model=UsageResponse` now filters the output.
- **src/api/routes/jobs.py**
  - `_run_in_flight` filters by owner (`Job.user_id == user_id`), so the `job_id` in a 409 is always the caller's own job.
  - The IntegrityError handler matches only SQLSTATE 23505 on `uq_jobs_one_active_per_config` and re-raises anything else. After the rollback, the re-read still filters by user_id and fails closed: `job_id` is None, still a 409.
  - The pre-check runs before the entitlement and quota gates but only produces a 409; it never skips a gate on the path that creates a job.
  - `cancel_job` is now in `writes`, finished-log replay in `general`, and `export-url` and `download` in `export`.
  - **The S3-07 rewrite is sound:**
    - `?token=` is strictly decoded against the `bridgeleads-download` audience only, requires `purpose=download` and a matching `job_id`, and checks the jti blacklist and logout-all.
    - A Redis error returns 503, never a pass.
    - The user must be `is_active`.
    - The header path now goes through `get_auth_context`, which checks blacklist, logout-all and session family, rejects refresh tokens, and applies the Business-plan gate for API keys.
    - After authentication the RLS GUC is set, and the Job read still filters by `user_id`.
- **src/api/routes/scrapers.py** — create, patch, delete and csv-layout moved to `writes`, and each route has a `request: Request` parameter (checked on 352, 459, 542, 844). The delete still filters by owner and now also clears `paused_reason` (data caveat: D4-1).
- **src/api/schemas.py** — `RunEligibilityResponse` and `UsageResponse` only add response models. The consistency validator cannot fail for any value `run_eligibility` produces. No internal ids, provider metadata or other tenants' data.
- **src/config/settings.py** — The cap settings are `int | None`, and a validator rejects negative values at boot. Unset or 0 still means disabled; that is S3-12's default-off, unchanged, not worse. A leftover `SKIP_TRACE_DAILY_ROW_CAP` is now read as credits, which is stricter, not looser.
- **src/db/models.py** — `Job.holds_run_slot(now)` is a pure SQL predicate. Callers add the user filter themselves, or run in the system session where the config id is unique per tenant. The frontier `Index` declaration matches migration 103.
- **src/workers/batch_tasks.py**
  - The config query is still owner-pinned (`user_id == batch.user_id`) and adds the child clause.
  - An entitlement-paused child is always blocked, whatever `ENTITLEMENT_ENFORCEMENT` says.
  - The "still stopping" and "already running" lookups are pinned to config and user.
  - Each insert runs in its own SAVEPOINT that catches only the 104 unique violation (`pgcode 23505` and the exact constraint name) and re-raises anything else. The `FOR UPDATE` lock on the run row is held throughout.
  - The quota gate before the fan-out is unchanged.
- **src/workers/scheduler_helpers/dispatch.py** — The dispatch blocker now uses `holds_run_slot`. The new lookup that runs on skip is used only for logging, and runs in the system session keyed on the unique config id. No change to what gets dispatched, apart from the 5-minute cooldown after a cancel.
- **src/workers/skip_trace_capacity.py** (new)
  - All SQL uses bind parameters. `discover_accounts` uses `text()` with `:trace_type` and `:watermark`. `allocate` and `lock_allocated` pass arrays through `bindparam` and `cast`. No string interpolation, and ORDER BY is fixed.
  - `credits_for` raises on an unknown type instead of pricing it at 1 credit. `_weight_sql` has no ELSE, so a type outside the known set would count as NULL; migration 102's CHECK constraint prevents such types.
  - `row_allowance`, `batch_rows` and `take_within_caps` cannot exceed the caps. The per-row cost is the pass's own type, `max(0, …)` handles an overspent account, and when the global cap is off the pass is still bounded by `BATCH_ROW_LIMIT`.
  - `lock_allocated` re-checks `status='queued'` and the trace type under `FOR UPDATE SKIP LOCKED`, which closes the retype-to-cheaper hole.
  - The `SET LOCAL enable_bitmapscan` runs inside a savepoint.
- **src/workers/skip_trace_dispatcher.py**
  - **Lock scope:** `pg_try_advisory_xact_lock` is taken per pass at line 234. Everything between it and the claim commit at line 499 makes no commit: `_cancel_undeliverable` and `_fail_unsubmittable` are documented not to commit and I verified they don't; `allocate` uses savepoints only. The only other transaction ends are `db.rollback()` on the "global cap full" and "no rows" branches, and both then `continue` to a new pass that takes the lock again.
  - **Spend reads:** spend is read after the lock is taken, in READ COMMITTED, and each earlier claim committed its `submitted_at` before releasing the lock. So two passes cannot claim the same allowance, and there is no count-then-insert race.
  - **Other spend paths:** `submit_batch` is called only from this function (lines 502 and 545). The caps are the single choke point for Tracerfy spend.
  - **Tenant isolation:** candidate joins are pinned to (id, user_id) for both Job and Result, and the lateral is keyed on `acct.c.user_id`. Writes stay tenant-paired.
  - **DoS bounds:** at most 12 rounds and a 2 s deadline between rounds, and each round's SQL has a LIMIT. `_InFlightCache` can only be stale in the safe direction: it can hold a row back, never let a second purchase through.
  - **402 partial path:** `affordable_row_count` now raises on an unknown type.
- **src/workers/tasks.py** — `skip_reason_for_config` runs after the ownership CAS, on a freshly re-read config inside the RLS session. It fails the job through `_fail_job`, which releases the reservation. This is a new fail-closed backstop on every path that starts a run.
- **Frontend c7d78fb** (BillingTab.tsx, types.ts, generated types) — Display only. `usage.plan` is used for a badge and gates nothing. No `dangerouslySetInnerHTML`; values are rendered as React text.
- **Frontend d4ba581** (scrapers/page.tsx, api.ts)
  - `runInFlightJobId` reads `job_id` only from a 409 with `code === "run_in_flight"`, and only accepts a string or null.
  - The page then calls `getJob(id)`, an owner-scoped API call, and routes to `/live/${job.id}` using the id the server returned. There is no HTML sink, and redirects can only go to internal paths.
  - No security logic lives only in the client.

## Audit #3 items: does the delta change them?

- **S3-03 (trial skip-trace spend, P1): not fixed. Unchanged in effect.** The delta has no trial or plan gate on the enqueue side: `enrich.py` and `registration.py` are not touched. The per-account credit cap now works correctly under the claim lock. It would limit what one trial can spend per rolling 24 h, but it is off by default (`SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP=None`), and when on it applies the same way to trial and paid accounts. A trial is still `plan='pro'` and can buy lookups that are never billed. S3-03 stays open at P1.
- **S3-09 (missing rate limits, P2): mostly fixed.** Every route S3-09 named is now limited:
  - download and export-url: `export`
  - cancel: `writes`
  - scraper create, patch, delete, csv-layout: `writes`
  - finished-job log replay: `general`, where it had no limit before

  `export` and `writes` keep an in-process limit during a Redis outage. Residual: batch and segment CSV exports still sit in `general`, see D4-2. Log replay in `general` still fails open while Redis is down.
- **S3-12 (global soft cap, P2): fixed in code, still off by default.** The caps are now in credits (normal 1, advanced 2), both global and per account, enforced inside the advisory lock for each pass. They are hard: no overlapping tick or batch can claim past them. One tenant cannot use up another's per-account allowance. They are still disabled unless production sets `SKIP_TRACE_DAILY_CREDIT_CAP` and `SKIP_TRACE_ACCOUNT_DAILY_CREDIT_CAP`. Not verified against the Railway env, per the no-production-access rule. The remaining risk is operational (set the variables), not in code.
- **S3-07 (logged-out tokens still download CSVs, P2): fixed.** `?token=` accepts only job-bound download tokens (strict audience, purpose and job_id checks), so a session JWT there gets a 401. The Authorization header goes through `get_auth_context`, which now covers the session-family check the old code missed, plus the blacklist, logout-all and the `is_active` user. Redis failure returns 503. The 60 s / 48 h download-token residual above is by design.
