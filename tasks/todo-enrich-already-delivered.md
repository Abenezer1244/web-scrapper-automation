# Enrich already-delivered leads on a later skip-trace run (2026-09-18)

Worktrees: BE `C:/Users/Windows/bl-wt-enrich` (`fix/enrich-already-delivered`, off origin/main ab14cae)
FE `C:/Users/Windows/bl-wt-enrich-fe` (`fix/enrich-already-delivered`, off origin/master 7e6c5de)

## Owner report
Run 1, skip trace OFF: 38 leads delivered, no phone/email. Run 2, same range, skip trace ON:
"38 already delivered", and none of the 38 was skip traced. Delivered and enriched are two
separate facts; dedup must stop re-delivery, not stop enrichment.

## Root cause (traced, file:line on ab14cae)
1. `src/workers/tasks_helpers/enrich.py:1986-1997` `_enqueue_skip_trace_rows` selects only
   `Result.is_duplicate.is_(False)`. Every already-delivered row is dropped before the cache
   check and before the queue. Comment at 1978: "a duplicate is never delivered or billed as a
   lead, so paying Tracerfy for it is pure waste" (from #296 D1/D5).
2. `enrich.py:181-305` `_reuse_enrichment_for_duplicates` copies contacts onto a duplicate only
   from the ORIGINAL row (`delivered_records.first_result_id`) and only when that original is a
   settled `hit`/`miss`. Run 1's original is `not_attempted`, so nothing is copied, and (1)
   then excludes the row for good.
3. The dispatcher re-applies the same rule three times, so even a queued duplicate is withdrawn:
   `skip_trace_dispatcher.py:179` (submit query), `:485` (`_cancel_undeliverable_queued`),
   `:707` (`_partition_still_deliverable`, `is_dup` -> drop).

## What already exists (no new schema needed)
- `results.skip_trace_status`: `not_attempted | queued | submitted | hit | miss | errored | purged`
  (`constants.SkipTraceStatus`). `miss` = Tracerfy answered, no contacts. `errored` = technical
  failure / unmatched. `not_attempted` = never asked. The spec's six states map 1:1; phone/email
  NULL is never used as state.
- Per-tenant `skip_trace_cache` (key = sha(user_id, address, city, state)), 90-day TTL
  (`SKIP_TRACE_CACHE_DAYS`, the established freshness rule; cache rows are purged at 90 days too).
  Ingest writes hit AND miss into it; errored writes nothing.
- Skip-trace billing = pending rows `completed`/`unmatched` of a real Tracerfy queue at ingest
  (`skip_trace_usage.report_usage_from_webhook`). A cache hit makes no pending row, bills nothing.
- Record quota counts `is_duplicate = false` rows only (tasks.py ~2027). The plan cap ranks
  non-duplicates only. Enriching a duplicate cannot touch record quota.
- Retry policy: 429/5xx/connection -> stays `queued`, next tick; definite rejection -> `errored`;
  unknown outcome -> held `submitting` for the reconciler. A later run makes a fresh
  `not_attempted` row, so an `errored` lead is retried by the next run with skip trace on.
- CSV (`jobs.py` download, `category=already_delivered`) is built LIVE from this job's rows with
  the configured layout (Phone 1..3 / Email 1..3). Enriching the rows fixes the CSV; no change.
- FE results page polls every 30s while any row on the tab is queued/submitted; query key
  includes the category. The tab refreshes itself as lookups land.

## Design
Eligibility = "this run delivers it" OR "an earlier run of this account delivered it"
(`results_category.already_delivered_condition()`: `is_duplicate AND
coalesce(duplicate_reason,'prior_run')='prior_run'`). Same-run siblings (`same_run`) and
`superseded` rows stay excluded: they are the same property as a row this run already traces.
`is_duplicate`, `duplicate_reason`, counts, billing, dedup claims: untouched.

Per already-delivered row, when the run has skip trace ON:
- A. never traced, nothing reusable -> queued -> normal paid lookup (1 skip-trace unit).
- B. a settled trace within 90 days exists for this account -> copied, no Tracerfy call, no charge.
- C. last attempt errored -> nothing reusable (errored is never cached/copied) -> queued, retried.
- D. last attempt was a `miss` within 90 days -> `miss` copied, no call. Past 90 days it is
  re-asked, same rule as every other lead.
Skip trace OFF -> nothing changes (the enqueue returns before selecting).

Idempotency / concurrency (a new race this change creates: run 2 can queue lead X while run 1's
lookup for X is still at Tracerfy):
- Reuse source widened: the latest settled `hit`/`miss` of ANY row of this account with the same
  strong `dedup_hash` inside the TTL, not only `first_result_id`. Belt beside the address cache.
- Dispatcher, before claiming: (a) settle any queued row whose tenant cache entry is now fresh
  (copy contacts, pending row -> `reused`, never billed); (b) hold back a row whose
  (user, cache key) is already `submitting`/`submitted`; (c) inside one batch keep one row per
  (user, cache key), the rest stay queued and settle through (a) next tick. Held rows follow the
  in-flight row's outcome: hit/miss -> reused; errored/unmatched -> submitted next tick (retry).
- A transaction-scoped advisory lock around select -> claim commit, so two overlapping ticks
  cannot both pass (b). Try-lock: a tick that cannot get it defers to the next tick.
- Key includes user_id: nothing ever coalesces or copies across tenants.

UI: the results API returns a contact-lookup summary for the already-delivered bucket (counts by
outcome, plus how many were answered from an earlier lookup with no new charge). The Already
delivered tab shows one line under its explainer when skip trace touched any of them. Real
counts only, no em dash.

## Phases (<=5 files each, verify + owner OK between phases)
- [ ] P1 eligibility: enrich.py enqueue predicate + widened reuse; dispatcher 3 gates use the
      same predicate; retarget the 3 existing "duplicate is withdrawn" fixtures to `same_run`
      (their real intent: a survivor demoted by re-election); new test file for the matrix.
- [ ] P2 idempotency: dispatcher cache-settle sweep, in-flight hold, in-batch coalescing,
      advisory lock; tests incl. two jobs + one lead -> one paid row.
- [ ] P3 API: summary field on ResultsPage (schemas.py, jobs.py, openapi.json) + test.
- [ ] P4 FE: summary line on the Already delivered tab; regenerate api types; Playwright
      (Chromium, not Claude in Chrome) against a local API: run 1 off, run 2 on, tab + CSV.
- [ ] P5 Codex review (11 owner questions) + security Master Review §14, journal, PRs.

## Test matrix (real DB, tests/test_skip_trace_already_delivered.py)
1 new + OFF -> delivered, no trace · 2 dup + OFF -> no trace · 3 dup + ON never traced -> queued ·
4 dup + ON prior hit -> copied, no pending row · 5 prior miss -> miss copied, no pending row ·
6 prior errored -> queued · 7 new + ON -> queued · 8 mixed 10/20/8 -> 10 new, 28 dup, 30 queued,
8 reused · 9 two jobs one lead -> one submitted row · 10 tenant A/B same parcel -> no copy ·
11 record-quota delta 0 for dups; skip-trace units only for real lookups · 12 CSV has the contacts.

## Decisions / disagreements (Codex plan consult, 17 findings, each checked against code)
ADOPTED
- One predicate everywhere: `already_delivered_condition()` (and its SQL twin) in enqueue,
  reuse targets, dispatcher submit query, cancel sweep and partition.
- Widened reuse: TARGET = this job's already-delivered rows only (never same_run/superseded);
  SOURCE = settled hit/miss of the same account + same strong dedup_hash, inside TTL, NOT this job.
- Billable-failure retry (Codex P1, correct): `errored` conflates a definite pre-submit rejection
  (never charged) with `unmatched` (Tracerfy charged, we could not attribute). Auto-retrying the
  latter each run would re-buy the same failure. Rule: an already-delivered lead whose account has an
  `unmatched` pending row for the same property inside the TTL is marked `errored`, not re-queued;
  a held coalesced row whose in-flight twin ends `unmatched` is settled the same way. A lead whose
  last attempt was a transport error / pre-submit rejection IS retried.
- Delivery contract made explicit + tested: lookups for already-delivered rows are bought only once
  the CURRENT run is delivered (existing `_job_delivered_sql`). Run fails/cancels -> rows cancelled
  back to `not_attempted`, next run retries.
- `reused` pending status: terminal, never billed (billing whitelists completed/unmatched only;
  verified every status check in src/ is a positive list); tests for reconcile/cancel interplay.
- New code checks cache freshness as `fetched_at` inside [now-TTL, now+5min], not `age.days`.
- Summary provenance: "looked up in this run" = a pending row of THIS result reached Tracerfy
  (completed/unmatched); every other hit/miss = reused. Not-traced (no owner name, placeholder
  address, policy) is its own count, so the numbers always add up to the tab's total.
- Tests assert pending-row/submitted counts and usage rows, not only Result status.
REJECTED (with evidence)
- "Advisory lock is not exactly-once; crash after POST double-pays": already solved upstream. The
  claim is committed as `submitting` BEFORE the POST and an unknown outcome is never auto-resubmitted
  (dispatcher.py:240-275, reconciler). The lock only serializes the coalescing check.
- Cross-tenant attribution by address: pre-existing, unchanged by this fix, measured (1 collision
  ever, same tenant); ambiguous attribution is already refused, never guessed. Nothing here
  coalesces or copies across tenants (every key includes user_id).
- trace_type in the cache key: the existing cache is trace-type agnostic by design; coalescing
  follows the cache's own semantics so behaviour stays consistent. Batches are per trace_type.
- Unique constraint on pending (result_id): each Result belongs to one job and one enqueue; the
  dispatcher's per-(user, key) coalescing also collapses any accidental twin before payment.
- Overlong-field cache-key drift, check-then-insert cache race, `db.add` try/except, NULL
  `duplicate_reason` backfill: pre-existing, unrelated to this bug; logged as follow-ups, not
  folded in (NULL reason = pre-089 rows, documented as prior_run by design).

## Review
(filled in at the end)
