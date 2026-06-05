# Phase 4 — Tax filters (build)

**Branch:** new `feature/phase4-tax-filters` off `main`. **Migration head:** 037 → next 038.
**Spec:** `docs/superpowers/specs/2026-06-04-lead-targeting-delivery-design.md` (§ Phase 4).
**Goal:** filter `tax_delinquent` leads by amount owed (min/max $) + time delinquent (months). **KING FIRST** (only King has structured $ + tax year; Pierce/Snohomish/Kitsap = recorder keyword matches, no amount/age).

## Verified facts
- King scraper emits `ScrapedRecord.enrichment_data = {source:"king_county_delinquent_taxes", delinquent_amount: float$, bill_year: str, billed_amount, paid_amount, ...}`.
- `tasks.py` insert (~line 506) bulk-inserts Result rows from ScrapedRecord incl `enrichment_data` JSON. Result has NO structured amount/age columns.
- Billing (`tasks.py` ~563): `billable = len(records) - dup_count` at SCRAPE time, before any filter.
- `get_results` = per-job results view (filters job_id + user_id). ScraperConfig JSON cols: fields/enrichment/schedule/deliver/doc_types.

## Codex consult (done) — reconciled
- 4A: `delinquent_amount NUMERIC(12,2)` + `delinquent_bill_year INTEGER`. Derive months at QUERY time from bill_year (King bills 01/01/year) — don't store volatile months; don't overclaim exact duration.
- Populate **SOURCE-GATED** (record_type=tax_delinquent AND enrichment_data.source="king_county_delinquent_taxes"), NOT "if keys present". Coerce `Decimal(str(v))`, quantize cents, reject negative/NaN/absurd; bill_year int in 1900..current+1.
- Indexes: partial `(job_id, delinquent_amount) WHERE NOT NULL` + `(job_id, delinquent_bill_year) WHERE NOT NULL` (per-job view-filter path).
- Backfill idempotent + source-gated + keyset.
- **Billing decision: ship option B (view/export filter, NO billing change) FIRST.** Defer scrape-time filter + post-filter billing redesign. Matches low-risk-foundation-first pattern.

## Slice 4A — structured King tax columns (data foundation, ≤5 files, decision-independent)
- [ ] Migration **038**: `results.delinquent_amount NUMERIC(12,2) NULL` + `results.delinquent_bill_year INTEGER NULL` + 2 partial indexes. Additive, no in-migration backfill.
- [ ] `models.py`: Result columns + indexes (import Numeric).
- [ ] `tasks.py`: `_extract_tax_fields(enrichment_data, record_type)` (source-gated, coerced) → populate the 2 columns in the insert dict.
- [ ] `scripts/backfill_result_tax_fields.py`: offline, source-gated, idempotent, keyset by id, from enrichment_data JSON.
- [ ] Verify compile/ruff/import + tests for the extractor (malformed/negative/cents/bad-year). Codex review → commit.

## 4A status: ✅ BUILT + Codex-reviewed (gate pass). Committed c6bd358. 17 tests pass.
- **Codex [P2] — deploy-order race (DOCUMENTED, not coded around):** workers don't run migrations; a worker restarting with this code before the API applies 038 would hit undefined-column. Same pattern as Phase 2a `doc_type` (shipped fine) + self-healing via Celery `max_retries=3`/30s. **MERGE-TIME REQUIREMENT:** API applies 038 on boot before workers reach steady state; transient worker failures retry and heal. Not merged yet → no current risk.

## Slice 4B — view/export tax filter: ✅ BUILT (user chose option B — view/export, no billing change)
- [x] `src/api/tax_filters.py` (pure, tested): `bill_year_bounds_for_months` (months↔bill_year, server date) + `build_tax_conditions` (SQLAlchemy predicates; NULL rows excluded when a filter is set).
- [x] `get_results`: query params min/max_amount + min/max_months → ANDed into the paginated view query (`total`+`items` reflect filter; job-level scrape stats stay unfiltered).
- [x] `download_export`: same params → filtered export; empty filtered set returns header-only CSV (not 404, which stays for genuinely-empty jobs). Added `delinquent_amount` + `delinquent_bill_year` CSV columns.
- [x] `ResultRow`: surfaced `delinquent_amount`/`delinquent_bill_year`.
- [x] Inclusive bounds; non-King-tax rows (NULL cols) never match a set filter. 12 filter tests (29 Phase-4 total).
- [ ] Codex review of 4B (next). UI gating (show inputs only for King tax configs) = frontend repo.

## DECISION FOR USER (before 4B): where does the filter apply + billing?
- **(B, Codex-recommended)** view/export filter — scrape/bill everything, filter what's shown/exported. Low risk, no billing change. User pays for all scraped.
- **(A)** config/scrape-time filter — only deliver matching leads, MOVE billing post-filter. HIGH risk (quota/retry/idempotency semantics).

## Constraints
- No local test DB → build + Codex-verify + CI roundtrip. Migration 038 additive = low risk. Merge → deploy on boot.
- Non-King tax sourcing (Pierce/Snohomish/Kitsap amount+age) = separate research spike, NOT this slice.
