# Batch overlaps-first delivery (spec + plan: docs/superpowers/{specs,plans}/2026-07-01-*)

Branch `chore/xcheck-session` (worktree off origin/main @ 5bc4b74), draft PR #136.
Executed subagent-driven: fresh implementer + reviewer per task; no local Postgres, so
tests verified via GitHub Actions CI on every push; Codex consulted at design (2 rounds)
and gates the final diff.

## Plan

- [x] Task 1 — Migration 078 + model columns (`delivery_mode` on scraper_batches,
      `delivery_counts` on batch_runs) — `38243c1` (+ ruff import-order fix `eae4287`)
- [x] Task 2 — `_COMBINED_SQL` rework: prefixed type-scoped buckets, property_key-only
      overlap identity, SQL-side mode filter + deterministic ORDER BY/LIMIT/OFFSET,
      uncapped `_DELIVERY_COUNTS_SQL` — `c9eddcd` (fixes Bugs A & C)
- [x] Task 3 — mode-aware `finalize_batch_run` + `delivery_counts` stored + empty-state
      email gating (`_delivery_summary`; `_deliver` gates on done/partial) — `1a01ab9`
- [x] Task 4 — `deliver_job_email`/`_build_lead_delivery_email` optional
      `summary_message` + `link_expires` (batch emails lose the wrong "expires in 48
      hours" copy) — `9b2e543` + byte-identity fix `6552702`
- [x] Task 5 — API: persist `delivery_mode` on create, **status-based readiness**
      (`_DOWNLOADABLE_STATUSES`), mode-aware download rebuild, response fields —
      `8eac41e` (fixes Bug B) + OpenAPI regen `3f753fc`
- [x] Task 6 — paginated `GET /batches/{id}/leads` + `/runs/{run_id}/leads`
      (async RLS session, live counts, no-store, hidden-fields) — `e52edb5`
      + lazy-import fix `17fbb7e` + review-minors fix (uniform no-store,
      run-scoped tenant test)
- [x] Task 7 — docs (spec §7 amendment, this file, BUILD_JOURNAL), full CI, security
      self-review, Codex review gate

## Review

**The three pre-existing bugs fixed:**
- **Bug A (fake overlaps):** weak `dedup_hash` (party_name+date) could merge two
  DIFFERENT record types into one bucket — counted as a hot overlap AND silently
  dropped one row. Now `dh:` buckets are record_type-scoped; only `property_key`
  bridges types.
- **Bug B (empty export = 404):** `combined_export_key` gated readiness, and zero-row
  finalizes never set it — paid batch, no email, download 404. Readiness is now
  status-based (done/partial); zero-row runs stream a headers-only CSV and email the
  honest empty-state summary.
- **Bug C (50k cap before filter):** mode filtering/ordering moved into SQL before
  LIMIT; honest counts come from a separate uncapped aggregate.

**Feature:** per-batch `delivery_mode` = `overlaps_only` (default for NEW batches;
existing batches backfilled `everything` — no behavior change on deploy) |
`overlaps_first` | `everything`; honest `delivery_counts`
{leads_total, overlaps_delivered, singletons_suppressed, unmatchable_no_parcel};
paginated combined-leads JSON for the in-app one-list view.

**Security self-review (Master Review scope for this diff):**
- Every new query binds `:uid` from an ownership-verified batch/run; `/leads` chains
  rate_limit → `_owned_batch` (404 non-owner) → run-membership check → RLS session.
- Decrypted PII only in page rows; `Cache-Control: no-store` on every leads-route
  path; no row payloads logged anywhere.
- No new secrets; no user-supplied URLs; CSV path still goes through
  `build_overlap_export_row`/`sanitize_for_csv`.
- Migration additive-only; `CHECK` constraint guards non-API writers.

**Notes / deviations:**
- Spec §7 amended (see spec): readiness re-keyed to run status instead of forcing an
  empty-file R2 PUT (the object is never served; API has no R2 creds).
- Tertiary CSV sort changed filing-date → job-recency (SQL date-parse of M/D/YYYY
  strings would break the export on garbage rows).
- CSV ordering now fully SQL-side; `_filing_sort_key` (batch_export copy) deleted.
- Deploy order: run migration 078 via `scripts/migrate.py` BEFORE deploying api+worker;
  redeploy BOTH (delivery email kwargs span worker+api).
- Frontend follow-up (separate repo, backend-first): wizard mode picker, batch-page
  combined leads table + counts banner, regen TS types from schema/openapi.json.
