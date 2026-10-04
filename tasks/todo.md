# Batch system audit + redesign (2026-10-04)

Branch `audit/batch-system-redesign`, BE worktree `C:/Users/Windows/bl-wt/batch-audit`, base `a4d13234`.
Audit findings: `docs/audits/2026-10-04-batch-system-audit.md` (Codex consulted, concurs; P1 on new-vs-delivered).
No merge, no deploy without owner OK. Each phase <= 5 files, verified, then owner approval.

## Decisions (owner said "Start" on the recommendations, 2026-10-04)
- [x] D1 keep all rows; split new vs already delivered; badge rows; filter
- [x] D2 Skip tracing OFF = option A (no new paid lookups, own prior answers reused free, always disclosed)

## Phase 1 (BE): honest batch counts + contact provenance  [P1 fix]
- [x] `_COMBINED_CTES`: provenance columns on candidates; `has_new` per bucket in agg
- [x] `_QUALITY_SQL` (separate statement, one pass over the shared ranked CTE; `_DELIVERY_COUNTS_SQL` left as-is because its dict is persisted and asserted exactly)
- [x] `BatchLeadRow`: `already_delivered`, `skip_trace_status`, `skip_trace_attempted_at`, `contact_reused` (raw source stays internal)
- [x] filter `?delivery=new|delivered` (shared `_VIEW_FILTERS`, page and pager cannot drift; CSV binds NULL)
- [x] tests: batch A vs B, forged foreign-tenant job id, new/delivered split + reconciliation, filter + page 2, 422 on bad value, reused provenance, skip OFF = reused + 0 pending rows. County collision / leading zero / hyphen / missing parcel were ALREADY pinned in test_property_identity.py
- [x] openapi regenerated (additive only), ruff clean, no BE type checker configured
- [x] Codex review: 2 P1 + 5 P2 raised; verified: P1 NULL is_duplicate (column NOT NULL in DB) and P1 CSV widening (DictWriter fieldnames whitelist) rejected with evidence; PACS P2 was my 2-dot diff against a moved origin/main (not this branch); blank-contact P2 rejected (blank stored as NULL); accepted: quality doc clarity, 4th-query ceiling note, page-2 test
- [ ] full suite (4 batches, isolated DB `bridgeleads_batchaudit_test`)
- NOTE: phase touched 3 src + 4 test files (+ openapi, todo); tests are consequence of the SQL bind contract

## Phase 2 (BE): run summary API for one-row-per-batch
- [ ] batch list response: status rollup (queued/running/complete/complete with issues/failed/cancelled), children[] (county, record type, status, new, already delivered), markets, record types, skip-trace state from execution data (off / on / mixed / reused-only), quality state
- [ ] tests: 1/2/10-child, multi-county, failed/cancelled/mixed, child not double-counted

## Phase 3 (FE): dashboard + navigation
- [ ] dashboard uses standalone scrapers + batches (one row per batch, `batch_id`, no name matching); section renamed "Recent runs"
- [ ] batch runs findable (filter/search by county, record type, child name)

## Phase 4 (FE): batch detail redesign
- [ ] overview first (unique properties, stacked, single-list, new vs already delivered), leads, quality checks, scrapes, activity; contact provenance chips; explicit CSV scope
- [ ] mobile cards at 320-1440, a11y pass, Playwright verification

## Phase 5: Codex review gate + security Master Review + journal

# Results run summary UX (2026-10-03)

Branches `fix/results-run-summary-ux` in BE worktree `C:/Users/Windows/bl-wt/run-summary-be` (base `76c8fa08`) and FE worktree `C:/Users/Windows/bl-wt/run-summary-fe` (base `9ee1f9b6`). No merge, no deploy.

Owner report: "Complete · 0 new / Of 71 records found: 71 No address 0 New leads" over a table saying "No records found."

## Verified so far (from code)
- The six-bucket partition already exists (`src/api/run_breakdown.py`): one SQL CASE, first match wins, so rows land in exactly one bucket. `_invalid()` rejects any snapshot whose buckets do not add up to `records_found`. A live read has `dropped_before_save=None` (no total) for retried runs or ones with no `records_found`.
- `no_address` = NOT (usable property_address OR usable mailing_address). The placeholder `(enrichment unavailable)` counts as no address. It is checked FIRST, so a no-address row is never counted as a duplicate or as over quota. It does NOT mean "parcel not found": the parcel can resolve while both addresses stay empty.
- `dropped_before_save` = living-owner Transfer-on-Death exclusions plus repeat filings merged at save time.
- The 71 rows are KEPT in `results` (lead_actionability.py). The list endpoint hides them through `actionable_condition()`, so there is no way to look at them today.
- The table's "No records found." describes the NEW-leads list, not the scrape.
- The prod trace of the 71-record run was BLOCKED (auto-mode denied production reads). Script ready: `scratchpad/diag_run71.py` (read-only, MIGRATE role, readonly session, no PII printed). Needs an owner run.

## Prod trace of the run (owner ran the read-only diag, 2026-10-03)
- Job `6b1f3445`: Island WA probate, rolling_90 (07/05 to 10/03), finished 05:50 UTC. Snapshot: found 71 = dropped 0 + no_address 71 + merged 0 + delivered 0 + over_quota 0 + new 0. It reconciles, and record/billed count is 0.
- All 71 rows: parcel_id NULL, property NULL, mailing NULL, no placeholder, no duplicates. The recording index has no parcel (EagleWeb parcel-less county). 37 probate_death_inheritance + 34 tod_living_owner_estate_planning (TOD deed / PR deed / death certificate).
- The only address path is `enrich.py:1137` (PACS search by owner name). Log: "Found 0/71 addresses via PACS" in 23 s. The code says Island searches take 10–18 s each, so 71 names / 5 workers should take minutes. The 23 s points to fast failures.
- **ROOT CAUSE (probed the public portal 2026-10-03):** the search POST now answers `302 → SearchResults.aspx`, and an unrelated GET then hit `302 → customdisplay.htm?aspxerrorpath=` (ASP.NET error page). `lookup_pacs_by_name` posts with `allow_redirects=False` and returns None on any non-200 (pacs.py:348). Every exception is also None (pacs.py:361). So "portal changed/erroring" is indistinguishable from "no match", and the run logged "Enrichment complete: addresses added".
- Verdict: **71 is correct; 0 is NOT a trustworthy answer**. It is a silent enrichment failure, not proof the records lack addresses.
- Side defect: `doc_type` stores "Transfer on Death Deed\n4606292" (the instrument number is glued on).

## Codex consult (high) — GATE FAIL, reconciled
- P1 the no_address list/count must bypass actionable_condition → planned (own branch + own count query)
- P1 the superseded population → the no_address list uses the partition's first branch exactly, with no superseded exclusion
- P1 snapshot vs live drift (backfill) and P1 tax-cap drift → the summary shows the frozen number and the tab shows the live count. A one-line note appears when they differ. The tax cap stays on the list (owner rule: "never shown").
- P2 "couldn't be matched to a property" overclaims → the copy becomes "had no property or mailing address". The per-row reason comes from parcel_id: "Not matched to a parcel" / "No address on file for this parcel".
- P3 tenant tests + keep no_address out of the download/export/contact-lookup category → adopted

## Plan (owner approved 2026-10-03: separate PACS PR first; store per-row outcome; keep tax cap; no unmatched CSV)
- [x] 0. Owner ran the diag; root cause = PACS search redirect read as "no match" (see above)
- [x] PACS PR (branch fix/pacs-owner-search-redirect, worktree bl-wt/pacs-redirect, commit 9cae2a66): post_search follows the same-origin 302; found/no_match/failed; enrichment_data.pacs_name_lookup; honest step + completion lines
- [x] BE-1 results_category: ResultsListCategory + no_address_condition (list only)
- [x] BE-2 get_results: no_address branch, own no_address_count, schema field
- [x] BE-3 tests/test_results_no_address.py (Island 71/71, list == breakdown bucket on every row kind, tax cap, search, 422 on export/download/quote, tenant 404)
- [x] FE-1 types regen + ResultsCategory/ResultsListCategory split; lib/run-summary.ts + node:test (12)
- [x] FE-2 RunSummary / RunOutcomes (Live page too)
- [x] FE-3 page: one new-lead count, Run complete + icon, 3 tabs, CSV gating (csvHasRows vs tabHasLeads), EmptyNewLeads
- [x] FE-4 table copy per view, per-row reason, "No address" badge
- [x] V-1 tsc/eslint/next build; pytest subsets (PACS 1257 passed; UX 845 passed after supplying BL_TEST_REDIS_SERVER); Playwright 95/95 at 7 widths
- [x] V-2 Codex: consult FAIL->reconciled; PACS review FAIL->PASS; UX review NO-GO->(3 passes)->PASS

## Review
- 71 was right; 0 was not a trustworthy answer: a silent enrichment failure (PACS 302), now visible per row and in the run log.
- No new taxonomy: the six-bucket partition already reconciled. The UX now words it honestly ("had no property or mailing address", never "couldn't be matched"), puts new leads first, gives every outcome a way to inspect it, and stops saying "No records found" on a run that found records.
- Follow-ups: Island throttle (pacing + budget + deferral for the owner-name path); re-run 6b1f3445's records after the PACS PR deploys; FE PR must wait for the BE schema merge (types gate).

# Mailing-address forensic audit (2026-10-02)

Branch `audit/mailing-address-2026-10-02`, worktree `C:/Users/Windows/bl-wt/mailing-audit` (cut from origin/main `d10f95a6`; the main checkout is detached 562 commits behind, do not audit it).

Owner's question: for every supported WA county and record type, does BridgeLeads retrieve a legitimate owner/taxpayer mailing address when one is available? Evidence, not assumptions.

## Phases
- [x] 0. Fresh worktree from origin/main; read prior mailing history (memory + `docs/HANDOFF-mailing-address-2026-09-13.md`)
- [x] 1. Map architecture source -> UI (5 parallel read-only mappers: county_gis, enrich orchestration, persistence/API/export, scraper registry, recovery sweep)
- [x] 12. Read-only prod DB analysis (`scratchpad/dq_mailing_audit.py` via `railway run`, role postgres, transaction_read_only=on). Result: coverage is COUNTY-shaped, not record-type-shaped.
- [x] 4. Clark + Benton probate jobs pulled (ids, ranges, counts, logs, samples) — see report
- [x] 2/3. County x record-type matrices (code + prod config + official-source research) — see final report
- [x] 5. Real parcels verified: Clark 196948000 + 97976264 (PIC), Benton 131073011125003 (PACS: county publishes mailing, BridgeLeads NULL), Okanogan 9935250696 / 1250110000 / 3322070010 (TaxSifter: 1250110000 is county-owned, assigned to two leads = wrong inference), Thurston 74700001201 (A+)
- [x] 6/7/9/10/11. Confirmed from code: no property->mailing copy in enrichment (but the PACS grid parser WAS a situs echo: fixed); parcels never int-cast; skip trace gates only Tracerfy; dedup does not block later lookups; one column name end to end
- [x] 8. Sources probed: PACS forms identical on 6 portals (Island offline), Clark PIC 429 after ~10 fast requests, Chelan detail GET 500 without session, Whitman token-required, Kitsap psearch 500s
- [x] Phase 1 `86195315`: pacs_parcel.py adapter (7 PACS counties) + pacs.py situs-echo fix + detail parser; 72 tests
- [x] Phase 2 `c07d7c78`: thurston_assessor.py; 24 tests
- [x] Phase 3 `1d2c3d2a`: Okanogan never infers parcel from surname; TaxSifter owner addresses shared helper; 36 tests
- [x] Phase 4 `77a6a86c` + `e4f1d153`: workers/data_quality.py hourly sweep + ops alert + scripts/data_quality_report.py; 17 tests. Prod dry run: king code_violation 37014cb9 is a TRUE positive (3.5% mailing vs 65% baseline)
- [x] Phase 5 `a91da0db` (stacked on -76's 9ff6bceb): wire adapters into county_gis `_BULK_MAILING_SOURCES`; `has_mailing_source()` drives deferral + `mailing_missing`; recovery/backfill discover `parcel + NULL mailing` rows marker-independently (Snohomish tax 1,751)
- [x] Phase 6 `99673842`: backfill script (dry-run default, county-scoped, fill-only, outcome counts) — counts to owner, approval before apply
- [x] Phase 7: live E2E (pipeline -> DB -> API -> CSV): Benton 4/4 MATCH vs independent county read, Thurston filled, Clark deferred (PIC URL serves no record; reported to -76). Codex r1-r5, r5 GATE: PASS. Journal entry. UI rendering verified by code only (MailingValue renders the API value verbatim); no browser run
- [x] Codex consult with the evidence (scratchpad `codex_consult_out.txt`): root cause CONFIRMED fleet-wide; P1s to honor: PACS parcel→prop_id identity must be corroborated (explicit parcel_mismatch), never reuse the owner-NAME PACS parser for parcel enrichment, PACS "mailing" built from address lines may be a situs echo (reject mailing == property unless the page labels it mailing), provenance on every write, backfill discovers rows independent of deferred markers with dry-run + per-source outcome counts + rollback; DQ report needs outcome distributions not just coverage %.
- [x] Coordination: Clark PIC mailing is being built by session -76 (worktree wt-clark-mailing, uncommitted, Codex P1s in progress, no PR). Clark is OUT of my fix scope; I stack on its `_BULK_MAILING_SOURCES` / `has_mailing_source()` after it merges. Until then I touch only NEW files.
- [ ] CHECK-IN with owner: findings + proposed fix scope before any code change
- [ ] 13. Admin data-quality health report (per-run coverage %, baseline comparison, warning)
- [ ] 14/15. Fix at the right abstraction (county adapter for the counties that have a public source); never fabricate; fill-only writes
- [ ] 16. Historical backlog counts (done, see report) + idempotent backfill design (no quota, no skip-trace, resumable, rate-limited) — approval before running
- [ ] 17. Regression tests (list in owner prompt)
- [ ] 18. Browser E2E (Playwright, not Claude-in-Chrome) for Clark + Benton probate
- [x] Codex review r1-r5 (r5 GATE: PASS); journal; memory

## Working notes
- Prod kill switch `COUNTY_GIS_RESTRICTED_MAILING_ENABLED=true` on worker (read 2026-10-02) — not the cause.
- Clark probate job `62404bd0` (admin, 09/01/2025..09/30/2026): 1335 rows, 1335 parcel, 1292 property, **0 mailing**, 0 deferred markers. Logs never mention a mailing lookup; "Enrichment complete: addresses added".
- Benton probate job `bc8d507c` (admin, 07/04..10/02/2026): 7 rows, 4 parcel, 4 property, 0 mailing.
- Every 0%-mailing county (benton, chelan, clark, okanogan) is 0% on EVERY record type it has; king/pierce/snohomish/cowlitz are 56–100%. Snohomish tax_delinquent since 09-18: 1,751 rows, 0% mailing, parcel 100%, no deferred marker (pre-#346 rows, never retried).

## Review
Root cause: county coverage gap in the shared enrichment (no mailing source outside pierce/king/snohomish/cowlitz), plus a PACS situs-echo parser and Okanogan surname-invented parcels. Fixed on this branch (11 commits on top of -76's Clark commit); 475 targeted tests green; full suite NOT run (memory-reaped). Not merged, not deployed, no prod data changed. Next: -76 PR, this PR, backfill dry runs, owner approval per county.
