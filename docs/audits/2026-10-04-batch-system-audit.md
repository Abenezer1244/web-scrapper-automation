# Batch system audit (2026-10-04)

Forensics for the batch "testas" plus a code audit of the batch pipeline. All production reads ran on a
read-only session (`set_session(readonly=True)`); no row was written, no Tracerfy call was made.
IDs below are internal UUIDs, no PII.

## 1. What a batch is (actual model)

| Concept | Table / code | Notes |
|---|---|---|
| Batch | `scraper_batches` | name, state, `enrichment` (skip_tracing flag), `delivery_mode` (`everything` / `overlaps_only`) |
| Child scraper | `scraper_configs.batch_id` (tenant-scoped composite FK) | one per county x record type; inherits `skip_trace_enabled` from the batch |
| Batch run | `batch_runs` | `child_job_ids`, `status`, persisted `delivery_counts`, `failed_children` |
| Child scrape (job) | `jobs` (`trigger='batch'`) | normal job pipeline: scrape, dedup, enrich, bill |
| Result row | `results` | `is_duplicate` = already delivered in an earlier run; `property_key` = cross-match identity |
| Combined lead | `_COMBINED_CTES` in `src/workers/batch_export.py` | one row per identity bucket over all child rows |

A batch is all three of: a container of scrape jobs, a combined result set, and a cross-record-type
intersection engine. The parent relationship already exists in the backend (`scraper_configs.batch_id`,
`batch_runs.child_job_ids`); no new model is needed for grouping.

## 2. Screenshot batch forensics

- batch `dcd8243a-0fe1-4112-980f-aacedfcaa1d8`, user `b6d2095d-...`, created 2026-10-04 06:52:51 UTC
- `delivery_mode = overlaps_only`, `enrichment = {property_lookup: true, skip_tracing: false}`
- run `d6840085-...`, status `done`, running 06:52:52, completed 07:03:05
- persisted `delivery_counts = {leads_total: 385, overlaps_delivered: 1, singletons_suppressed: 384, unmatchable_no_parcel: 0}`

| Child | Job | Status | Result rows | New (`is_duplicate=false`) | Already delivered | UI shows | Skip trace requested | Attempted (pending rows) | Contacts on rows |
|---|---|---|---|---|---|---|---|---|---|
| Pierce pre-foreclosure | `2c5efebd` | done 07:02:43 | 295 | 16 | 279 | 15 new / 275 already delivered | no | 0 | 0 |
| Pierce probate | `c22e37bc` | done 06:55:54 | 108 | 1 | 107 | 1 new / 99 already delivered | no | 0 | 23 (all `reused`, all already-delivered rows) |

UI child counts are lower than the raw rows because the results view applies the actionability rule
(no property and no mailing address = not a lead) and the 18-month tax cap.

## 3. The 384

Source: `_DELIVERY_COUNTS_SQL` (`src/workers/batch_export.py:170`), persisted at finalize into
`batch_runs.delivery_counts`, read live by `_leads_page` (`src/api/routes/batches.py:879`).

It counts `pk:` buckets with one record type over `candidates`, and `candidates` is **every result row of
the batch's child jobs** (`r.job_id = ANY(:job_ids)`, user-pinned), after actionability + tax filters.
`candidates` has **no `is_duplicate` filter**, so the 386 already-delivered rows are inside it.

Raw (pre-filter) bucket check: 398 buckets, 381 contain only already-delivered rows, 17 contain a new row.
So the 384 is overwhelmingly properties the user already received in earlier runs.

Ruled out: other batches (query is pinned to this run's `child_job_ids`), other tenants (every join
carries `:uid`), stale cache (live count equals persisted count), frontend mapping (field maps 1:1).

**Defect (P1, Codex concurs):** the combined set, its counts, and the CSV never distinguish new from
already-delivered. The one "stacked" lead in this batch is made of two already-delivered rows; all 17
new leads were single-list and suppressed by `overlaps_only`. The page shows 1 + 384 next to children
showing 15 + 1, with nothing reconciling them.

## 4. Contact shown with skip tracing OFF

Row `ad19f990` (probate, `is_duplicate=true`): `skip_trace_status=hit`, `skip_trace_source=reused`,
`skip_trace_attempted_at=2026-09-27 05:22:14`, identical to row `2694c99e` of the SAME user's earlier
probate job `06a5c173` (skip trace ON, `source=lookup`, a paid Tracerfy answer).

Writer: `_reuse_enrichment_for_duplicates` (`src/workers/tasks_helpers/enrich.py:202`), called for
EVERY job at the start of enrichment (`enrich.py:871`) with no `skip_trace_enabled` check. It copies the
account's own settled answer (hit/miss, 90-day TTL, subject-hash match, strong parcel/address identity)
onto already-delivered rows, stamping `skip_trace_source='reused'`.

- New Tracerfy call: **no** (0 `pending_skip_trace_rows` for any row of either child job).
- Charge / quota: **none** (reuse never enqueues; billing counts new rows only).
- Cross-tenant: **no** (both legs pinned to `:uid`; the 2 other tenants holding this property never donate).
- The combined view then picks this row as the bucket representative because it ranks rows with contacts first.

Provenance IS persisted (`skip_trace_source` lookup / reused / NULL, `skip_trace_attempted_at`,
`skip_trace_subject_hash`, migrations 097/098). The batch API and UI drop it.

**Semantics:** current behavior is option A (no new paid lookups; reuse own prior answers free).
Recommendation (Codex concurs): keep A, and disclose it on every surface: "Existing contact, found
Sep 27" instead of a bare phone. States to expose: not requested, existing contact reused, newly
looked up, no contact found, lookup failed (all derivable from `skip_trace_status` + `skip_trace_source`).

## 5. Cross-match identity

`compute_property_key` (`src/workers/property_identity.py:79`): parcel-primary, scoped `STATE|county`;
address fallback (also county-scoped); weak identity = NULL (never grouped). Parcel normalization strips
hyphens/spaces, keeps leading zeros deliberately. Same parcel string in two counties cannot collide.
Owner name is never part of the key. Residual P3: identical normalized street address in two cities of
one county (address branch only).

## 6. Dashboard shows two rows for one batch

`app/(dashboard)/dashboard/page.tsx` feeds `ScrapersTable` from `listScrapers` (`GET /scrapers`, every
config including batch children). The Scrapers page already uses `listStandaloneScrapers` + `listBatches`
and renders one row per batch. The dashboard never adopted that. Fix is to use the existing relationship,
not name matching.
