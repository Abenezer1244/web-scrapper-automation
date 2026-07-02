# Batch Overlaps-First Delivery — Design Spec

**Date:** 2026-07-01
**Status:** Approved design, pending user review of this spec
**Branch:** additive worktree off `origin/main` @ `5bc4b74`
**Reviewers:** Claude (design) + Codex (2 consult rounds: concept @ medium, concrete plan @ high, UI endpoint follow-up @ high). All Codex P1/P2 findings adopted.

---

## 1. Product goal

The whole point of a batch scrape is **leads that appear in 2+ record types** (the
intersection = hottest motivated-seller signal). Singletons are noise obtainable from
single scrapes. Batch delivery — CSV **and** in-app UI — must center on overlaps.

**Product-owner decisions (final):**
- Per-batch `delivery_mode` = `overlaps_only` | `overlaps_first` | `everything`.
- **Default for NEW batches = `overlaps_only`**, with an honest empty-state.
- Empty state = **headers-only CSV** (download never 404s) + honest counts in email/UI.
- `delivery_mode` = **dedicated column** (not the `deliver` JSON blob).
- Batch page UI shows **one combined leads list** (not per-child outputs); overlaps
  listed prominently if any, honest message if none.

## 2. Bugs this fixes (pre-existing, found during design)

**Bug A — fake overlaps.** `_COMBINED_SQL` buckets on
`COALESCE(property_key, dedup_hash, 'id:'||id)`. `dedup_hash`'s weak branch is
`party_name + date_recorded` (`src/workers/tasks.py:596,610`), so two DIFFERENT-record-type
rows with the same name+date collide into one bucket: they get `overlap_count = 2`
(fake hot lead) AND one row is silently dropped from the export (rn=1 picks one
representative). `dedup_hash` "deliberately DIFFERS" from `property_key`
(`src/db/models.py:639`) — it is a billing dedup key, not a property identity.

**Bug B — empty export = 404 on a paid batch.** `finalize_batch_run` only uploads when
`pairs` is non-empty (`src/workers/batch_export.py:268`); `combined_export_key` gates
"ready" (`src/api/routes/batches.py:443`). Zero rows → no file, no email, download 404.
Today this is rare; with `overlaps_only` it would be common (parcel-weak counties).

**Bug C — 50k cap before filter/sort.** `_COMBINED_SQL` applies `LIMIT :limit` with no
`ORDER BY`; Python sorts after. Any mode-filtering done in Python after the cap could
return zero overlaps even when overlaps exist beyond the arbitrary 50k sample, and
counts would be sample-counts, not batch-counts.

## 3. Overlap identity (the core rule)

- A **real cross-type overlap** = bucket where `property_key IS NOT NULL` AND
  `count(DISTINCT record_type) >= 2`. `property_key` (parcel-primary, county/state-scoped,
  `src/workers/property_identity.py`) is the ONLY key that may bridge record types.
- `dedup_hash` still dedups **within** a record type but can never bridge types.
- **Bucket keys become prefixed + type-scoped:**
  - `'pk:' || property_key` (cross-type capable)
  - `'dh:' || record_type || ':' || dedup_hash` (within-type only — Codex P1)
  - `'id:' || id` (no identity — never groups)
- `overlap_count` = `count(DISTINCT record_type)` for `pk:` buckets; always `1` otherwise.
- **Known limitation (accepted):** parcel-less sources (court-filing probate, some
  EagleWeb, PACS fuzzy) can't cross-match; the honest counts make this visible instead
  of silent (`unmatchable_no_parcel`).

## 4. Delivery modes

| Mode | CSV / leads content |
|---|---|
| `overlaps_only` (new-batch default) | Only real overlaps (`pk:` buckets, overlap_count ≥ 2). Zero rows → headers-only CSV + counts message. |
| `overlaps_first` | Everything, overlaps ranked first (existing sort already does this once fake overlaps are gone). |
| `everything` | Current behavior (all deduped leads). Existing batches backfill to this. |

Mode filtering happens **in SQL** (WHERE on the overlap predicate), before
ORDER BY/LIMIT (fixes Bug C for the export path too).

## 5. Honest counts

Computed by a **separate uncapped aggregate query** (same CTEs, `count(*)` instead of
row selection — Codex P1) and stored on the run at finalize as `delivery_counts` JSON:

```json
{"leads_total": N, "overlaps_delivered": X, "singletons_suppressed": Y, "unmatchable_no_parcel": Z}
```

- `leads_total` = all deduped buckets; `overlaps_delivered` = real overlaps;
  `unmatchable_no_parcel` = non-`pk:` buckets; `singletons_suppressed` = `pk:` buckets
  with 1 record type.
- **Live reads (UI/download) recompute counts with the batch's CURRENT mode** — the
  stored blob is the as-delivered snapshot for the email/history, never blindly
  returned where the current mode governs (Codex P2).

## 6. Schema — migration 078

- `scraper_batches.delivery_mode VARCHAR(16) NOT NULL server_default 'everything'`
  + `CHECK (delivery_mode IN ('overlaps_only','overlaps_first','everything'))` (Codex P2).
  - Existing batches (incl. recurring scheduled) keep current behavior — no silent
    output change on deploy.
  - New-batch default `overlaps_only` lives in `BatchCreateRequest` (Pydantic), and
    `create_batch` **explicitly persists** `delivery_mode=body.delivery_mode`
    (Codex P1 — SQLAlchemy omits the column otherwise and the DB default wins).
  - `server_default` kept for deploy safety; all app writers must be explicit (Codex P3).
- `batch_runs.delivery_counts JSON NULL` — worker-written at finalize (compatible with
  the RLS cutover: app role is SELECT+INSERT only on batch_runs; worker does UPDATEs).
- Deploy via `scripts/migrate.py` (advisory lock); additive columns only — no table
  rewrite, no backfill job needed.

## 7. Worker changes (`src/workers/batch_export.py`)

- `_COMBINED_SQL`: prefixed/type-scoped buckets (§3); mode predicate + deterministic
  `ORDER BY` (overlap_count DESC, contactable first, job recency, id) + `LIMIT`
  **in SQL**; companion uncapped counts query. *(Amended: tertiary sort is job
  recency, not filing date — SQL date-parsing of the M/D/YYYY string column would
  error on garbage rows and break the whole export.)*
- `finalize_batch_run`: always finalizes with honest `delivery_counts`; uploads to
  R2 only when there are rows (the object is an ops artifact — downloads rebuild
  from the DB; the API has no R2 creds). **Readiness is status-based**
  (`done`/`partial`), not key-based — this is the ready-marker-path fix for Bug B
  (Codex P1): a zero-row overlaps_only run is downloadable (headers-only CSV,
  verified `write_lead_csv_with_overlap([])` emits headers) and emails its honest
  empty-state summary. *(Amended from "always uploads + always sets
  combined_export_key": forcing an empty-file R2 PUT would add a new failure mode
  — an R2 outage blocking empty-run finalize — for an object nothing ever reads.)*
- `_deliver` / `deliver_job_email`: new optional `summary_message` kwarg (default
  preserves existing per-job emails — Codex P2); batch email carries the counts line
  (e.g. "0 cross-type overlaps; 37 leads had no parcel to cross-match — view all in
  the app"). Fix the batch email's incorrect "expires in 48 hours" copy (it links to
  the app page, not a presigned URL).

## 8. API changes (`src/api/routes/batches.py`, `src/api/schemas.py`)

- `BatchCreateRequest.delivery_mode: Literal['overlaps_only','overlaps_first','everything'] = 'overlaps_only'`.
- Route persists it; download rebuild (`render_combined_csv` / `_stream_run_csv`)
  receives the batch's current mode (same semantics as hidden-fields pass-through).
- `delivery_mode` + `delivery_counts` surfaced on `BatchSummaryResponse`,
  `BatchDetailResponse`, `BatchRunResponse` (run responses get the batch plumbed in —
  Codex P2; `_run_response` currently only receives the run).
- **NEW: paginated combined-leads JSON** (the in-app "one list" view):
  - `GET /batches/{batch_id}/leads` (latest run) and
    `GET /batches/{batch_id}/runs/{run_id}/leads` (history parity — mirrors CSV).
  - Same combined query + mode + hidden-output-fields as the CSV; rows mirror CSV
    columns + `matched_record_types`/`overlap_count`/`source_counties`; response
    includes the live-computed counts summary.
  - **Gated on `combined_export_key`** (same ready-marker as CSV — no partial mid-run
    rows; Codex P2). Auth `CurrentUser` + `_owned_batch`; rate-limited;
    `Cache-Control: no-store`; page_size ≤ 100; SQL LIMIT/OFFSET pagination.
  - Runs on the **async RLS session** (precedent: per-job results + segments return
    decrypted contacts via `get_rls_db`); decrypt only the returned page.
  - Performance: re-running the aggregate per page is acceptable at current scale
    (Codex: pragmatic GO; materialize only after measured pain).

## 9. Frontend (sibling repo `bridgeleads-web` — SEPARATE follow-up PR, backend-first)

- Batch page (`app/(dashboard)/batches/[id]/page.tsx`) gains the combined leads table
  fed by `/leads`: overlap badge ("Probate + Tax Delinquent"), counts banner, honest
  empty-state ("0 cross-type overlaps found; M leads couldn't be cross-matched —
  switch mode to see all N").
- Wizard gains the delivery-mode picker (default Overlaps only).
- Per-child "Scrapes" list stays as a status/progress panel; run history unchanged.
- Types regenerate from the BE OpenAPI (pinned `.venv-schema`; merge backend first).

## 10. Testing (real `TEST_DATABASE_URL`, no mocks)

1. **Overlap identity:** same parcel across 2 record types → 1 row, overlap_count=2;
   same name+date across 2 types WITHOUT parcels → 2 rows, no overlap (Bug A regression).
2. **Modes:** one seeded dataset → assert row sets for all three modes; overlaps_first
   ordering puts overlap rows first.
3. **Empty state:** overlaps_only with zero overlaps → finalize sets
   `combined_export_key`, CSV is headers-only, `delivery_counts` correct, email enqueued.
4. **Counts:** leads_total / overlaps_delivered / singletons_suppressed /
   unmatchable_no_parcel add up; uncapped counts vs capped export divergence covered.
5. **Migration/back-compat:** existing batch rows read `everything`; new API batch
   persists `overlaps_only`; CHECK constraint rejects garbage.
6. **/leads endpoint:** tenant isolation (user B 404s), ready-gate (pending run 404s),
   pagination determinism, hidden-fields blanking, counts in response.

## 11. Phases (≤5 files each, verify + user approval between)

- **Phase 1 — core correctness (worker + migration):** migration 078, models.py,
  batch_export.py (buckets/mode/counts/always-upload), tests. *Bug A/B/C fixed here.*
- **Phase 2 — API surface:** schemas.py, routes/batches.py (persist mode, download
  mode pass-through, /leads endpoints), delivery.py email kwarg + copy fix, tests.
- **Phase 3 — FE (separate repo/PR, after BE merges):** wizard picker, batch page
  combined table + counts banner + empty-state.
- Each phase: self-review + security pass (multi-tenancy, PII, rate limits), then
  `codex review` of the diff. Any Critical/High from either reviewer = NO-GO.

## 12. Out of scope (deliberate)

- Improving parcel enrichment coverage for parcel-less sources (separate initiative —
  it raises overlap match-rate but is its own project).
- Materializing combined results at finalize (only if measured p95 pain).
- Address-based fallback overlap matching (risk of false positives; revisit with data).
