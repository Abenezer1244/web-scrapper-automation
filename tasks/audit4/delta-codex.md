# BridgeLeads DELTA security audit

Scope: `DELTA.diff` and the surrounding `src/` code for the changed skip-trace capacity/dispatcher, quota and run eligibility, billing usage, job and batch routes, entitlements, rate limiting, models, migrations 103/104, batch tasks, and scheduler dispatch. No tests, database connections, or repository commands were used.

## Findings

### CXD-1 — P1 — CONFIRMED

**Worker execution bypasses the new account-level run-eligibility gate.**

Location: `src/api/quota.py:187-236`; `src/workers/tasks.py:550-571`; `src/workers/tasks.py:1787-1840`.

The new `run_eligibility()` correctly rejects frozen accounts, ended entitlements, and exhausted quota, and the API enqueue gates call it. However, after a queued job is claimed, the worker checks only the scraper configuration and plan entitlements. It never calls `run_eligibility()` before scraping. The later reservation SQL locks the user and grants whatever quota remains, but it does not reject a now-frozen or now-ended account.

Exploit/failure scenario: a job is queued while the account is eligible; Stripe then freezes the account or `entitlement_ends_at` passes while the job is delayed. The worker claims it and performs scraping, and can reach quota reservation and skip-trace dispatch despite the account no longer being allowed to start billable work. A delayed scheduled, batch, or retry job has the same behavior. The worker also loads the user before the claim and does not refresh it for this decision.

Fix: after the ownership claim, reload the user and call the shared eligibility function with one current timestamp before any scrape or enrichment. Fail the job without external work when it returns `frozen`, `ended`, or `over_limit`; release any reservation. Add the same frozen/ended predicate to the atomic reservation statement as defense in depth so a state change between the preflight and reservation cannot authorize billing.

### CXD-2 — P1 — CONFIRMED

**A free trial is represented as Pro and can spend Tracerfy credits without a paid skip-trace entitlement.**

Location: `src/api/routes/auth_helpers/registration.py:181-193`; `src/api/routes/batches.py:229-232,371-375`; `src/workers/tasks_helpers/enrich.py:2280-2292`; `src/api/billing/skip_trace_usage.py:485-498`.

Registration stores a new trial user with `plan="pro"`. Batch creation therefore permits `skip_trace_enabled`, and child configs persist that flag. The worker's only skip-trace plan check rejects exactly `starter`; it does not check `trial_ends_at`, an active paid subscription, a Stripe meter item, or any other paid entitlement. The dispatcher can consequently submit the trial's misses to Tracerfy. A missing Stripe customer is handled only later by holding the meter event; it does not undo the provider lookup already performed.

Exploit/failure scenario: an unauthenticated person creates repeated trial accounts, enables skip tracing on Pro-sized batches, and causes paid/operator Tracerfy lookups while having no paid subscription or customer to charge. At minimum, one trial can spend the entire trial/Pro allowance because there is no trial-specific skip-trace budget in this gate.

Fix: define an explicit paid skip-trace entitlement and require it both when creating/enabling a config and immediately before dispatcher enqueue. If trials are intended to receive some lookups, give them a separate small atomic trial budget enforced under the same capacity/claim lock; do not use “plan is not starter” as the allow condition. Also reject or hold no-customer paid work before the external Tracerfy request, rather than discovering that condition during Stripe metering.

### CXD-3 — P2 — CONFIRMED

**The one-run guard can release a scraper's slot while its cancelled worker is still executing.**

Location: `src/db/models.py:817-844`; `src/api/routes/jobs.py:453-460`; migration `alembic/versions/104_jobs_one_active_per_config.py` (partial unique index covers only active statuses).

Cancellation immediately changes the job to `cancelled` and sets `finished_at`. `Job.holds_run_slot()` protects a started cancelled job only for a fixed 300 seconds. Migration 104's unique index no longer applies once the status is cancelled. A worker can be in a slow external scrape, enrichment, or cancellation-unaware section for longer than that window; after five minutes, POST, scheduler, or batch fan-out can create a new run for the same config.

Exploit/failure scenario: cancel a long-running job, wait just over the cooldown, then start the config again. The old worker and new worker overlap and can both perform external work or write/deduplicate leads. The comments in the model acknowledge that the old worker can continue after cancellation, so the cooldown is not an authoritative completion signal. This creates duplicate provider cost, data races, and a possible cross-run denial of service through repeated overlapping work.

Fix: represent the run slot with a durable worker-owned lease/attempt. Cancellation should set `cancel_requested` and leave the slot occupied until the worker acknowledges terminal state; only that worker (or a verified lease-expiry watchdog) may release it. Use a heartbeat/lease with an explicit stale-worker recovery path instead of a fixed post-cancel grace period.

### CXD-4 — P2 — CONFIRMED

**The new export rate-limit zone does not cover either batch CSV download route.**

Location: `src/api/middleware/rate_limit.py:42-48`; `src/api/routes/batches.py:730-736,844-867`; `src/api/routes/batches.py:755-767`.

The new `export` zone is documented and applied to job download/export-url flows, but both `/batches/{batch_id}/download` and `/batches/{batch_id}/runs/{run_id}/download` use the looser `general` zone (60 requests/minute). Each request rebuilds the complete CSV in a threadpool, opens a synchronous database connection, reads/decrypts the selected jobs, and buffers the full response. Batch size is bounded but still allows up to the configured 50,000-row export.

Exploit/failure scenario: an authenticated tenant repeatedly requests a large completed batch export at the general limit. The requests can occupy threadpool workers and database capacity, starving other tenants; the new export limit does not prevent it. The attack needs only the tenant's own completed batch and valid authentication.

Fix: use the `export` zone for both batch download routes, with one shared per-user bucket across all export endpoints. Add an in-flight export semaphore or cost-based limit and prefer persisted/streamed artifacts so repeated requests cannot rebuild and buffer the entire export concurrently.

## Checked and found clean

- Job, batch, batch-run, config, export-URL, and download queries inspected in the changed routes carry the authenticated `user_id` predicate or a user-owned parent join. Batch failed-child IDs and job response fields are owner-scoped; no confirmed IDOR or cross-tenant response disclosure was found.
- Skip-trace global/account capacity accounting is serialized under the per-trace-type transaction advisory lock. Weighted normal/advanced credits, `submitted_at` claim accounting, deliverability rechecks, `user_id`-paired in-flight keys, and definite-failure claim release were traced; no confirmed cap bypass or provider replay/double-charge bug was found in the changed dispatcher path.
- Dispatcher and batch-task raw SQL uses fixed SQL fragments with bound values. User-controlled identifiers are not interpolated. Migrations 103/104 use fixed index/table names and structural catalog checks; concurrent build and lock-timeout handling was inspected. No SQL injection, migration privilege gap, or new-table RLS gap was found.
- Migration 104's active-status unique index, batch child savepoints, scheduler occurrence uniqueness, and the batch hard ceilings/deduplication were checked. The cancellation-slot weakness is reported separately because the database index itself is correct for the statuses it covers.
- Billing usage computes effective limits/window state from the current user and validates the response against that state; it does not expose other tenants' usage or internal billing identifiers.
- Auth, webhook, Stripe, export, and write rate-limit fallback behavior is bounded/fail-closed in the inspected middleware; scraper write routes and job cancellation use the write zone.

No additional suspected findings are included; the four findings above are the issues traced to an actionable exploit or failure path.

DONE
