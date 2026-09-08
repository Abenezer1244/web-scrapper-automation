# Duplicate scope and "already delivered" audit (2026-09-08)

Branch: `fix/duplicate-scope-audit` (BE), `fix/duplicate-scope-copy` (FE)
Worktrees: `~/bridgeleads-worktrees/dup-scope`, `~/bridgeleads-web-worktrees/dup-scope`

## Verdict on the report

**There is no cross-account leak.** Verified in code, in production data, and live
in the browser. But the page that made the owner suspect one is genuinely broken:
its "prove it to me" link sends you to the wrong run.

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
  found"}` from the API, not a frontend hide. Verified live.

## The real defect

`previous_job_id` in `src/api/routes/jobs.py:538-573` picks "newest DONE job on a
sibling config with >=1 visible lead". It has **no bound requiring that job to
precede the job being viewed.**

Reproduced live on the exact reported page: the "View previous results" button
links to `/results/437ecba1` (ran 2026-09-08), two months AFTER the run being
viewed, and a run that delivered none of those 49 leads. The run that actually
delivered them is `fa573bfd` (2026-06-23).

So the banner made a true claim, then handed the user a link that disproved it.
That is the whole incident.

Secondary: the table renders `No records found.` directly under a banner saying
49 records were found and filtered. Two statements, one screen, apparent
contradiction.

## Findings from the Codex review (independently verified)

| # | Finding | Verified | Severity |
|---|---|---|---|
| 1 | `skip_trace_cache` is tenant-keyed (`user_id` is in `address_cache_key`), the `models.py:1088` docstring says address-only and is stale | `skip_trace.py:108` | Low (doc) |
| 2 | `previous_job_id` has no "created before" bound | `jobs.py:551` + live repro | **High** |
| 3 | Post-crash cleanup releases the quota reservation but NOT dedup claims | `tasks.py:257` | Medium |
| 4 | Plan-cap release deletes a claim whose hash is shared by a surviving delivered sibling row in the same job | `tasks.py:1509` | Medium |
| 5 | `trustee_sale` same-run sibling collapse also sets `is_duplicate=true`; the banner calls those "already received" although they never were | `trustee_sale_finalize.py:135` | Medium |
| 6 | `bridgeleads_app` has ALL privileges REVOKED on `delivered_records`, with a hard-fail verifier; a results-API read of the ledger is a non-starter | `provision_rls_roles.sql:146,177` | Blocker on design |

Production sizing of 3/4/5:
- 0 claims held by non-done jobs today (the release script has been run).
- 44,865 of 54,571 claims (82%) are dangling: `first_job_id` points at a deleted
  jobs row and `first_result_id` is NULL. Ledger attribution is unavailable for
  most history, which is why the fix must not promise per-run provenance.
- 33,522 duplicate-flagged rows have no surviving claim, but they are 1 user /
  2 jobs, all on 2026-09-04. Not systemic.
- 0 same-run collapse rows in production today (finding 5 is latent).
- 11 jobs schema-wide currently render an all-duplicate page.

## Plan

### Phase 1 (the reported bug) - BE
- [ ] `jobs.py`: bound `previous_job_id` to jobs created before the viewed job
- [ ] Tests: previous link never points forward; falls back when no earlier run
- [ ] `models.py`: correct the stale `SkipTraceCache` docstring

### Phase 2 (the reported bug) - FE
- [ ] Banner: say "already included in your previous results" and name the
      earlier run's date; do not assert a run we cannot prove
- [ ] Empty state: duplicate-aware line instead of `No records found.`
- [ ] Zero em dashes in modified copy

### Phase 3 (dedupe semantics, needs approval - touches the worker)
- [ ] Release dedup claims in the post-crash failure path (finding 3)
- [ ] Plan-cap release: skip hashes still held by a surviving deliverable
      sibling in the same job (finding 4)
- [ ] Distinguish same-run collapse from prior delivery so the copy is right
      for `trustee_sale` (finding 5)

## Review section

(to be filled in after implementation)
