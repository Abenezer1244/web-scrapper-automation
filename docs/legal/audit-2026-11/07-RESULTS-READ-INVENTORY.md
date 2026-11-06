# 07 - `results` read-path inventory (privacy suppression)

**Purpose.** List every code path that READS rows of the `results` table (`Result` in
`src/db/models.py`), so the new flag `results.privacy_suppressed_at` can be enforced on all
of them. A suppressed row (an opted-out person) must never be shown, exported, counted to a
customer, sent to a vendor, webhook or dialer, or have contact data bought for it.

**Date.** 2026-11-06. Read-only audit of the `compliance-be` worktree. No code was changed.

**Method.** Grep over `src/` and `scripts/` (tests and `__pycache__` skipped) for
`select(Result`, `select(Result.`, `sa_select(Result`, `select_from(Result`, `query(Result`,
`.join(Result` / `JOIN results` / `FROM results` (any case, any alias, `public.results`),
`USING results`, `aliased(Result`, `Result.<column>` inside `select(...)`, and every `text(`
SQL naming `results`. Then each hit was opened and the enclosing function and predicate read,
and helpers that build `Result` queries (`download_rows_select`, `tab_condition`,
`_COMBINED_CTES`, `PARTITION_SQL`, `read_partition`, `_enqueue_skip_trace_rows`) were followed
to their callers. Grep is not an AST: an ORM read written as `select(func.count()).where(Result...)`
without one of the spellings above can be missed. All such reads found in `src/` are listed;
for `scripts/` the per-file line lists come from the spelling grep and are not exhaustive.
UPDATE and INSERT statements are listed only when they also read rows (UPDATE ... FROM results,
INSERT ... SELECT FROM results).

**Predicates that will gain the clause (the plan).**

| Module | Predicate | Change |
|---|---|---|
| `src/api/lead_actionability.py` | `actionable_condition`, `actionable_sql`, `is_actionable`, `shown_condition`, `deliverable_before_cap_sql` | AND not suppressed |
| `src/api/lead_actionability.py` | `withheld_sql`, `withheld_condition` | OR suppressed |
| `src/api/results_category.py` | `list_condition` (all four categories, so `no_address_condition` and `not_qualified_condition` paths too), `download_rows_select` (via `shown_condition`) | AND not suppressed |
| not in the plan | `has_address_condition`, `address_actionable_sql`, `category_condition`, `skip_trace_eligible_*`, `qualified_sql` | unchanged |

Note: `address_actionable_sql` is used directly only by `deliverable_before_cap_sql` (covered),
the run breakdown CASE, and dedup claim release. `qualified_sql` is used directly by
`cv_late_delivery._ELIGIBLE`, which is why that path is a gap.

**Classes.** A = customer-visible rows or PII (API response, CSV/ZIP/R2 export, email,
webhook/dialer payload, vendor POST). B = customer-visible counts or aggregates only.
C = billing, quota or spend decision. D = internal pipeline, not emitted to customers.

**Verdicts.** COVERED = already applies a predicate that will gain the clause. GAP = needs the
clause (exact place given). DEFER-P4/P5 = send/write path the design assigns to a later phase.
N/A-internal = no customer effect.

PII columns abbreviated: name = `party_name`, heirs, prop = `property_address`, mail =
`mailing_address`, phone(s), email(s), ED = `enrichment_data`.

---

## Class A: customer-visible rows or PII

| # | file:line | function | reads | PII | predicate | verdict |
|---|---|---|---|---|---|---|
| A1 | src/api/routes/jobs.py:638 (+701 page fetch) | `get_results` | full `Result` rows into `ResultRow` | all | `list_condition(category)` always; `shown_condition()` for new/already_delivered (line 681); tax cap | COVERED (the no_address and not_qualified tabs rely on `list_condition` gaining the clause for ALL categories) |
| A2 | src/api/routes/jobs.py:1231 | `_attach_delivery_provenance` | originals' name, parcel, prop, mail copied onto already-delivered copies | name, prop, mail | `actionable_condition()`, tax cap | COVERED |
| A3 | src/api/routes/jobs.py:2598 (stream at 2700) | `download_export` | full rows streamed to CSV | all | `download_rows_select` -> `shown_condition()` | COVERED |
| A4 | src/workers/account_export.py:160 | `_build_zip` | full rows per job into the account ZIP | all | `download_rows_select` | COVERED |
| A5 | src/api/routes/segments.py:225 | `_INTERSECTION_SQL` (`intersection_preview` / `intersection_export`) | rows incl. phones/emails/heirs | all | `actionable_sql('r')`, tax cap; HAVING runs over the filtered candidates, so a suppressed row cannot make a property qualify | COVERED |
| A6 | src/api/routes/segments.py:333 | `_UNION_SQL` (`union_preview` / `union_export`) | same | all | `actionable_sql('r')` | COVERED |
| A7 | src/api/routes/segments.py:449 | `_INTERSECTION_DATED_SQL` | same | all | `actionable_sql('r')` | COVERED |
| A8 | src/workers/batch_export.py:94 (`_COMBINED_CTES`) | `_combined_pairs`, `render_combined_csv`, `finalize_batch_run`; `batches.py` `_leads_page` (1068-1110) via `_COMBINED_SQL`, `_FILTERED_TOTAL_SQL`, `_QUALITY_SQL`, `_FACETS_SQL` | combined batch CSV, R2 object and leads page | all | `actionable_sql('r')` in the single `candidates` CTE every statement builds on | COVERED |
| A9 | src/workers/tasks.py:1611 | `run_scrape_job` (first R2 export) | all job rows, filtered in Python | all | `is_actionable(res) and not res.is_duplicate` (1631) | COVERED |
| A10 | src/workers/tasks.py:2185 | `run_scrape_job` (post-enrichment re-export) | refetched rows, filtered in Python | all | `is_actionable` (2209) | COVERED |
| A11 | src/workers/scheduler_helpers/dialer.py:288 (count at 282) | dialer push sweep | name, phone, email, prop, mail into webhook/dialer | yes | `actionable_condition()`, tax cap, dialer-ready | COVERED |
| A12 | src/workers/dialer_outbox.py:139 | `process_dialer_outbox` | per-row live re-read before send | yes | `actionable_condition()`, tax cap | COVERED for the live re-read. Sealed outbox payloads: DEFER-P4/P5 |
| A13 | src/workers/cv_late_delivery.py:414 | late-delivery dialer push | one lead for `_send_to_dialer` | yes | `actionable_condition()`, tax cap, dialer-ready | COVERED |
| A14 | src/workers/skip_trace_dispatcher.py:2343 | `_build_payload_rows` (Tracerfy POST) | reads `pending_skip_trace_rows` copies, not `results` | name, prop, mail | candidate selection at :391 applies `not_(withheld_condition())` | DEFER-P4/P5 (selection is COVERED) |

## Class B: customer-visible counts or aggregates

| # | file:line | function | reads | PII | predicate | verdict |
|---|---|---|---|---|---|---|
| B1 | src/api/routes/jobs.py:696 | `get_results` `total` | count of A1 query | no | same as A1 | COVERED |
| B2 | src/api/routes/jobs.py:731 | `get_results` `enriched_count` | count of non-duplicate rows with prop | no | none (job, user, is_duplicate, prop only) | GAP |
| B3 | src/api/routes/jobs.py:746 | `get_results` `parcel_count` -> `enriching` flag | count | no | none | N/A-internal (only drives a boolean banner; unsure it matters) |
| B4 | src/api/routes/jobs.py:815 | `get_results` `counts_row` (total_scraped, duplicates, new_leads, same_run, already_delivered, contact buckets) | counts | no | `shown_condition()` in WHERE | COVERED |
| B5 | src/api/routes/jobs.py:867 | `get_results` no_address / not_qualified / late counts | counts | no | `list_condition(...)` (+ `shown_condition()` for late) | COVERED (needs `list_condition` clause for no_address and not_qualified) |
| B6 | src/api/routes/jobs.py:904 | `get_results` duplicate_sources | counts per source job | no | `shown_condition()` | COVERED |
| B7 | src/api/routes/jobs.py:1018 | `get_results` previous_job_id EXISTS | existence | no | `actionable_condition()` | COVERED |
| B8 | src/api/routes/jobs.py:1052 | `get_results` `has_auction_data` probe | existence | no | none | GAP (low) |
| B9 | src/api/routes/jobs.py:1071 | `get_results` `auction_coverage` (matched / awaiting / no_notice) | counts | no | none (job, user only) | GAP |
| B10 | src/api/run_breakdown.py:88 `PARTITION_SQL` (API via jobs.py:1103 `read_partition_async`; worker via finalize.py:221) | live run breakdown and done-time snapshot | bucket counts | no | `partition_case`: `new` uses `actionable_sql`, earlier branches use `address_actionable_sql`, `already_delivered_sql`, `qualified_sql` | GAP (see Gaps 3) |
| B11 | src/api/routes/jobs.py:2636 | `download_export` empty-job probe | existence | no | `has_address_condition()` | N/A-internal (decides 404 vs header-only CSV, no row data) |
| B12 | src/api/routes/analytics.py:120, 138, 154, 191 | `analytics_summary` trend, by_record_type, by_county, skip_trace | counts | no | `actionable_condition()` in shared `base` | COVERED |
| B13 | src/api/routes/batches.py:528 | `_execution_facts` `looked_up` | count of `skip_trace_source='lookup'` rows per child job | no | none | GAP (low; owner decision whether historic paid lookups of suppressed rows stay counted) |
| B14 | src/api/routes/batches.py:545 | `_execution_facts` coverage | counts | no | `actionable_condition()` | COVERED |
| B15 | src/api/routes/batches.py:789 | `get_batch` per-child counts | counts | no | `shown_condition()` | COVERED |
| B16 | src/workers/batch_export.py:335 `_DELIVERY_COUNTS_SQL` (batches.py:1088, `compute_delivery_counts`:585) | batch delivery counts | counts | no | `_COMBINED_CTES` (`actionable_sql`) | COVERED |
| B17 | src/api/routes/segments.py:528 | `_EXCLUDED_NO_DATE_SQL` | count | no | `actionable_sql('r')` | COVERED |
| B18 | src/api/contact_lookup_planner.py:244 | `tab_status_counts` (quote buckets) | counts | no | `tab_condition` -> `actionable_condition()` | COVERED |
| B19 | src/workers/scheduler_helpers/public_cache.py:66 and :75 | `refresh_public_sample_cache` | cross-tenant count of rows with prop, distinct counties | no | none | GAP (low; public marketing number) |
| B20 | src/workers/tasks_helpers/enrich.py:2575 | `_run_inline_enrichment` "N records have no deliverable address" job log | counts in customer-visible job log | no | none (Python address test) | GAP (low) |
| B21 | src/workers/cv_late_delivery.py:196 (`_PENDING_SCOPE`, used by :206 and :213) | `notify_late_deliveries` | ids and counts for an in-app notice | no | late stamp only | GAP (low) |

## Class C: billing, quota or spend decisions

| # | file:line | function | reads | PII | predicate | verdict |
|---|---|---|---|---|---|---|
| C1 | src/api/contact_lookup_planner.py:294 | `plan_tab_window` (quote) | name, parcel, prop, mail, ED for classification | yes (in memory) | `tab_condition` -> `actionable_condition()` | COVERED |
| C2 | src/api/contact_lookup_planner.py:312 | `count_remaining` | count | no | `tab_condition` | COVERED |
| C3 | src/api/routes/jobs.py:1797 | `confirm_contact_lookups` INSERT ... SELECT FROM results | ids | no | job + user only | COVERED downstream (C5 re-checks every quoted row with `actionable_condition()` before any claim); optional hardening in Gaps 9 |
| C4 | src/workers/contact_lookup_action.py:372 | `_claim` quoted-set integrity | counts | no | none | N/A-internal |
| C5 | src/workers/contact_lookup_action.py:384 | `_claim` | full rows + `enqueue_eligible` column | yes | `actionable_condition()`, `skip_trace_eligible_condition()` | COVERED (a suppressed row gets a non-claim verdict) |
| C6 | src/workers/contact_lookup_action.py:450 | `_claim` lost-race status re-read | id, status | no | none | N/A-internal |
| C7 | src/workers/tasks_helpers/enrich.py:3073 and :3282 | `_enqueue_skip_trace_rows` (scrape path enqueue, before and under the job lock) | full rows | yes | `actionable_condition()`, `skip_trace_eligible_condition()` | COVERED |
| C8 | src/workers/tasks_helpers/enrich.py:2856 | `settle_charged_unanswered` | dedup hashes of rows passed in | no | inputs already gated by C5/C7 | N/A-internal |
| C9 | src/workers/skip_trace_claim.py:688 | `claim_skip_trace_rows` INSERT ... JOIN results | ids, status | no | tenant/job join + `skip_trace_status = claimable` only | GAP (defense in depth; the single chokepoint every buy goes through, see Gaps 2) |
| C10 | src/workers/skip_trace_dispatcher.py:391 | `_candidates` | queued rows to submit | via pending copy | `skip_trace_eligible_condition()`, `not_(withheld_condition())` | COVERED |
| C11 | src/workers/skip_trace_dispatcher.py:903 | `_cancel_undeliverable_queued` | queued rows to cancel | no | `withheld_sql('r')` | COVERED (will also withdraw rows suppressed after enqueue) |
| C12 | src/workers/skip_trace_dispatcher.py:1099 | `_settle_queued_from_known_answers` UPDATE guard | id | no | `not_(withheld_condition())` | COVERED |
| C13 | src/workers/skip_trace_dispatcher.py:1395 | `_partition_still_deliverable` | flags | no | `withheld_condition()` | COVERED |
| C14 | src/workers/tasks.py:2000 | `run_scrape_job` quota reservation `want` | count | no | `deliverable_before_cap_sql('results')` | COVERED |
| C15 | src/workers/tasks_helpers/plan_cap.py:82 | `mark_over_quota_rows` | ranked ids | no | `deliverable_before_cap_sql` | COVERED |
| C16 | src/workers/tasks_helpers/finalize.py:221 | billing `billable_count = read_partition(...).new` | counts | no | `new` = `actionable_sql` | COVERED for the bill; snapshot side is B10 |
| C17 | src/workers/cv_late_delivery.py:105 (`_CANDIDATES_SQL`) and :127 (`_LOCK_ROW_SQL`) | `deliver_late_code_violations` | ids, dedup hash; then bills ONE record and removes the hold | no | `_ELIGIBLE` = `qualified_sql` + hold mark, not actionable | GAP (bills a suppressed row, see Gaps 1) |
| C18 | src/workers/cv_late_delivery.py:306 | `_CONTACTS_PENDING_SQL` -> skip trace request | ids | no | late stamp; the enqueue it calls applies C7 gates | COVERED downstream |
| C19 | src/workers/tasks_helpers/dedup.py:900 | `transfer_undelivered_claims` | group columns | no | `actionable_sql('results')` | COVERED (behaviour change: a suppressed row can no longer take or keep a claim) |
| C20 | src/workers/tasks_helpers/dedup.py:1002 and :1022 | `_transfer_one_claim` | claim holder + per-row `actionable` | no | `actionable_sql('r')` | COVERED (same note as C19) |

## Class D: internal pipeline (src/)

| # | file:line | function | reads | PII | predicate | verdict |
|---|---|---|---|---|---|---|
| D1 | src/api/routes/jobs.py:1222 | `_attach_delivery_provenance` page hashes | id, dedup_hash of rows already on the page | no | page ids | N/A-internal |
| D2 | src/workers/tasks.py:1365 | dedup step 1 fresh rows | id, hash, parcel, prop | prop | job + user | DEFER-P4/P5 (run_scrape_job insert/dedup) |
| D3 | src/workers/tasks.py:1478 | UPDATE ... FROM results src (prior-run flag) | ids | no | ids | N/A-internal |
| D4 | src/workers/tasks.py:1873 | post-enrichment refetch (feeds cap, re-export filter, membership upsert) | full rows | yes | none here; consumers filter (A10) | N/A-internal. Membership upsert of suppressed rows: DEFER-P4/P5 |
| D5 | src/workers/tasks.py:2051 | UPDATE results sib FROM results capped | ids | no | ids | N/A-internal |
| D6 | src/workers/tasks_helpers/enrich.py:337, :377 | `_reusable` duplicate reuse candidates | id, parcel, prop, full row for subject key | yes | job + user | N/A-internal |
| D7 | src/workers/tasks_helpers/enrich.py:444, :500/503 | duplicate enrichment/contact copy UPDATE ... FROM results ro | prop, mail, phones, emails of prior row | yes | tenant | DEFER-P4/P5 (writer; copies contact data onto a new row) |
| D8 | src/workers/tasks_helpers/enrich.py:849 | `_SQL_SETTLE_NOT_CARRIED` | EXISTS | no | parcel | N/A-internal |
| D9 | src/workers/tasks_helpers/enrich.py:1197 | `_run_inline_enrichment` all rows | full rows | yes | job + user | DEFER-P4/P5 (enrichment) |
| D10 | src/workers/tasks_helpers/enrich.py:1576 | PACS name lookup history | hash, max(created_at) | no | tenant | N/A-internal |
| D11 | src/workers/tasks_helpers/dedup.py:300/308 | `release_capped_dedup_claims` | EXISTS | no | `address_actionable_sql('keep')` | N/A-internal |
| D12 | src/workers/tasks_helpers/dedup.py:679, :757 | `collapse_same_run_siblings`, `reconcile_same_run_survivors` | groups | no | job | N/A-internal |
| D13 | src/workers/tasks_helpers/dedup.py:971 | `settled_claim_hashes` | claim holder | no | tenant | N/A-internal |
| D14 | src/workers/tasks_helpers/dedup.py:1215-1220 | `release_parcelless_no_address_claims` | EXISTS | no | `address_actionable_sql` | N/A-internal |
| D15 | src/workers/trustee_sale_finalize.py:127, :168, :233, :251, :261 | trustee-sale finalize and dedup | id, ED, groups, counts | ED | job | N/A-internal |
| D16 | src/workers/nts_matcher_task.py:115, :173 | NTS matching | id, parcel, prop, name | yes | record type / job | DEFER-P4/P5 (nts upsert) |
| D17 | src/workers/owner_recovery.py:149, :188, :220 | owner recovery | candidates | yes | various | DEFER-P4/P5 (recovery writers) |
| D18 | src/workers/mailing_recovery.py:164, :184, :623, :642 | mailing recovery | candidates | yes | various | DEFER-P4/P5 |
| D19 | src/workers/cv_mailing_recovery.py:99 | CV mailing recovery | candidates | yes | various | DEFER-P4/P5 |
| D20 | src/workers/cv_owner_recovery.py:160, :172 | CV owner recovery | candidates | yes | various | DEFER-P4/P5 |
| D21 | src/workers/pierce_cv_owner_recovery.py:98, :111 | Pierce CV owner recovery | candidates | yes | various | DEFER-P4/P5 |
| D22 | src/workers/property_recovery.py:112, :124 | property recovery | candidates | yes | various | DEFER-P4/P5 |
| D23 | src/scrapers/enrichment/pierce_atip_owner.py:671 | UPDATE results r (owner write) | row being written | yes | ids | DEFER-P4/P5 |
| D24 | src/workers/data_quality.py:76, :91 | per-run coverage ops alert | presence counts | no | job | N/A-internal |
| D25 | src/workers/qualification_quality.py:99, 109, 116, 125, 136, 225, 233, 241 | gate invariants ops alert | counts | no | job / gate | N/A-internal (suggestion: add an `outbound_suppressed` invariant later) |
| D26 | src/workers/scheduler_helpers/retention.py:108, :113, :116, :140 | PII retention purge | ids, counts | no | eligibility | N/A-internal |
| D27 | src/workers/scheduler_helpers/contact_lookups.py:83, :292 | contact lookup action settle | status | no | action | N/A-internal |
| D28 | src/scrapers/enrichment/source_probe.py:73 | King canary parcels | parcel ids sent to the public King assessor as a probe | no | county | N/A-internal (unsure: a suppressed person's parcel id may be used as a probe; not personal data on its own) |
| D29 | src/workers/dialer_outbox.py:156 | existence re-check after A12 refusal | id | no | id | N/A-internal (logs "tax lead past 18-month cap" for a suppressed row, misleading only) |
| D30 | src/workers/cv_late_delivery.py:386 | `_DIALER_LOCK_ROW_SQL` | late stamp | no | id | N/A-internal |
| D31 | src/workers/tracerfy_ingest.py:897, :963; src/api/routes/webhooks.py (Tracerfy webhook); src/scrapers/enrichment/skip_trace.py `ingest_webhook_csv` | skip trace ingest | status / writes | yes | pending rows | DEFER-P4/P5 (ingest) |

## Class D (and exceptions): operator scripts in `scripts/`

Operator-run under `railway run`, never reached by a customer. Output goes to the operator's
console or JSONL backups. Grouped; line numbers are the spelling-grep hits.

| # | scripts | class | verdict |
|---|---|---|---|
| S1 | `backfill_skip_trace_jobs.py:195` | C (enqueues paid skip trace through `claim_skip_trace_rows`) | GAP (see Gaps 4) |
| S2 | `repair_orphaned_duplicate_flags.py:65, 78, 94, 249, 251` | B (rewrites `jobs.record_count`, the delivery headline, with a hand-copied actionable rule at 246-265) | GAP (low, see Gaps 8) |
| S3 | `fix_repaired_job_headline.py:24` | B (one-off for one hard-coded job, same hand-copied rule) | N/A (historical one-off; retire rather than patch) |
| S4 | `sprint4_three_counties_prod.py:184, 204` | D (starts real jobs; delivery goes through the gated pipeline) | N/A-internal |
| S5 | `backfill_nts_matches.py:87, 129`; `repair_nts_ts_number.py:293, 375, 494, 557`; `repair_trustee_sale_from_notices.py:50, 60` | D | DEFER-P4/P5 (nts upsert) |
| S6 | `requeue_gis_mailing_recovery.py:47`; `requeue_king_tax_owner_recovery.py:55`; `rerun_pierce_address_recovery.py:68, 90`; `repair_pacs_mailing_recovery.py:81` | D | DEFER-P4/P5 (recovery) |
| S7 | `repair_probate_party_and_bad_parcel.py:94, 100, 168` (rewrites queued pending payloads; the dispatcher still gates with `withheld`) | D | N/A-internal |
| S8 | `cleanup_watchdog_billed_dups.py:59, 123, 203, 463, 489, 496, 523`; `cleanup_watchdog_dup_results.py:33, 97, 112, 130, 315`; `release_stranded_dedup_claims.py:95, 155`; `repair_king_tax_historical.py:139` | C (only reduce charges or release claims) | N/A-internal |
| S9 | backfills writing lead facts: `backfill_assumed_mailing.py:95, 167`; `backfill_clear_king_tax_placeholder_names.py:68`; `backfill_duplicate_provenance.py:49, 69, 87`; `backfill_king_bulk_legal_mailing.py:61, 192`; `backfill_king_code_violation_mailing.py:33`; `backfill_king_code_violation_owner.py:76, 114`; `backfill_king_probate_current_owner.py:60`; `backfill_king_tax_owner_names.py:63`; `backfill_mailing_by_county.py:55`; `backfill_owner_flags.py:43, 74`; `backfill_pierce_code_violation_owner.py:62`; `backfill_pierce_statewide_mailing.py:58`; `backfill_preforeclosure_party_names.py:95`; `backfill_property_keys.py:44, 53, 63, 136`; `backfill_property_membership.py:43`; `backfill_property_situs_parts.py:47`; `backfill_result_property_key.py:8, 56`; `backfill_result_tax_fields.py:46`; `backfill_tax_legal_from_assessor.py:98, 209`; `backfill_tax_legal_stand_in.py:97`; `king_rpacct_mailing.py:60`; `king_taxbill_mailing_check.py:53, 69`; `repair_code_violation_party_name_dash.py:33`; `repair_king_property_situs.py:64`; `repair_placeholder_absentee.py:46`; `repair_stale_absentee_owner.py:61`; `verify_retention_ready.py:92`; `purge_test_batch_configs.py:163` | D | DEFER-P4/P5 (writers) or N/A-internal |
| S10 | diagnostics and reports: `_cutover_step3_rehearse.py`, `cv_qualification_report.py:70`, `data_quality_report.py:36`, every `diag_*.py`, `e2e_snoho_matcher.py`, `monitor_king_job_guard.py:41`, `spike_date_recorded_coverage.py`, `test_snoho_preforeclosure.py`, `ui_county_audit.py:133`, `verify_tracerfy_provenance.py:55, 66` | D (operator console only; several print PII to the operator) | N/A-internal |

## Outside the `results` table (noted, not counted)

- **Stored export artifacts.** The per-job R2 object (`exports/{user}/{job}/leads.*`, written at
  tasks.py:1651 and re-written at 2211) and the batch combined R2 object are snapshots taken at
  delivery. A later suppression does not change them. The in-app download and export-url read
  live rows (covered), but `src/utils/data_exporter.py:378-400` still falls back to a raw R2
  presigned URL for email delivery when the app link is not configured. Suppression needs either
  regeneration/deletion of affected R2 objects or a guarantee the presigned fallback is off.
- **Derived PII copies.** `pending_skip_trace_rows` (name, addresses), `skip_trace_cache`,
  `property_list_membership.property_address` (upserted from all rows, dedup.py:74) and dialer
  outbox payloads hold copies that the `results` flag does not reach. `src/workers/membership_query.py`
  reads `property_list_membership` but has no importer in `src/` (unused).
- **Predicate builders, not read sites:** `results_sort.py`, `tax_filters.py`, `owner_filters.py`,
  `dialer_filters.py`, `lead_qualification.py`, `utils/lead_export.py` (writer of rows it is handed).

## Site counts

| Class | Sites (src/) | of which GAP |
|---|---|---|
| A | 14 | 0 (1 DEFER, rest COVERED) |
| B | 21 | 8 (B2, B8, B9, B10, B13, B19, B20, B21) |
| C | 20 | 2 (C9, C17) |
| D | 31 | 0 (12 DEFER-P4/P5, D4 partly DEFER, rest N/A-internal) |
| scripts | 10 groups | 2 (S1, S2) |

A row can stand for several statements in one function (for example A8, B12, D25), so
"sites" counts functions/statements as tabled, not individual SQL strings.

## Gaps

1. **cv_late_delivery.py:98-100 (`_ELIGIBLE`), used by `_CANDIDATES_SQL` :105 and `_LOCK_ROW_SQL` :127.**
   Late delivery removes the hold and BILLS one record using `qualified_sql` only, so a suppressed
   held code violation is charged and stamped delivered. Fix: append
   `AND r.privacy_suppressed_at IS NULL` to `_ELIGIBLE`. (Severity: billing for a lead the
   customer can never see.)
2. **skip_trace_claim.py:688-696 (`claim_skip_trace_rows` insert).** Every paid lookup is claimed
   here, but the statement trusts its callers for eligibility. Fix (defense in depth): add
   `AND r.privacy_suppressed_at IS NULL` to the `WHERE` after `r.skip_trace_status = :claimable`
   (line 696). A refused row simply is not in `RETURNING`, which callers already treat as
   "not won".
3. **run_breakdown.py:64-81 (`partition_case`), read by `PARTITION_SQL` :86.** Once
   `actionable_sql` gains the clause, a suppressed non-duplicate row fails every branch and falls
   to `unclassified`, which makes `breakdown_from_job` / `live_breakdown` reject the run's
   snapshot (run_breakdown.py:184, :286); suppressed rows in the `no_address`, `same_run_merged`
   and `already_delivered` branches are still counted to the customer. Fix: add a FIRST branch
   `WHEN {alias}.privacy_suppressed_at IS NOT NULL THEN 'suppressed'` plus a `suppressed` field in
   `RowPartition`/`ROW_BUCKETS` that the API does not render, or exclude suppressed rows in
   `PARTITION_SQL`'s WHERE and make the `records_found` reconciliation tolerate them. Which of the
   two fits the snapshot columns (`SNAPSHOT_COLUMNS`) is unsure and needs a design call.
4. **scripts/backfill_skip_trace_jobs.py:195-200.** Operator backfill that buys skip trace selects
   rows with no actionable/eligibility predicate. Fix: add `actionable_condition()` and
   `skip_trace_eligible_condition()` to that `where(and_(...))` (or at minimum
   `Result.privacy_suppressed_at.is_(None)`). Gap 2 also catches it.
5. **jobs.py:731-740 (`enriched_count`) and jobs.py:1071-1087 (`auction_coverage`).** Customer-visible
   counts with no predicate. Fix: add `Result.privacy_suppressed_at.is_(None)` to both `.where(...)`
   lists (or `shown_condition()` for `enriched_count`, which also aligns it with the list).
   Also jobs.py:1052-1060 (`has_auction_data` probe), low: same clause.
6. **batches.py:528-535 (`looked_up`).** Count of paid lookups per child job, no predicate. Fix:
   add `Result.privacy_suppressed_at.is_(None)` to the `.where(...)`, unless the owner decides
   historic paid lookups stay counted as billing history.
7. **public_cache.py:66-71 and :73-81.** Public, cross-tenant marketing counts. Fix: add
   `Result.privacy_suppressed_at.is_(None)` to both `.where(...)`.
8. **Low-impact counts.** (a) enrich.py:2577-2581: exclude suppressed rows from `unactionable`
   (`and res.privacy_suppressed_at is None`) before the job-log line at :2599. (b) cv_late_delivery.py:196-202
   (`_PENDING_SCOPE`): add `AND r.privacy_suppressed_at IS NULL`; note rows then stay
   `notified=false` forever, so either also mark them notified or accept the rescan.
   (c) scripts/repair_orphaned_duplicate_flags.py:246-265: replace the hand-copied rule with
   `actionable_sql('r2')` so the `record_count` headline matches billing.
9. **Optional hardening, jobs.py:1792-1800 (`confirm_contact_lookups`).** Not a leak (the worker
   re-checks), but confirming a quote whose lead was suppressed after quoting records it as
   quoted. Fix if wanted: add `AND r.privacy_suppressed_at IS NULL` to that INSERT ... SELECT so
   the existing `quote_stale` 409 fires.

Unsure items: B3 (`enriching` flag) and D28 (King probe parcel ids) are judged harmless; the
`scripts/` line lists may miss ORM reads not spelled as `select(Result...)`.
