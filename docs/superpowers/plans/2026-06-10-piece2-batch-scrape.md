# Batch Scrape — Implementation Plan (Piece 2, Phase 2A on-demand MVP)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax. Every phase: `ruff`/`pytest` + Codex review gate (`.claude/rules/codex-collaboration.md`); STOP for user approval between phases (`CLAUDE.md`).

**Goal:** Let a user pick multiple counties × record types in one "batch" run that fans out into N normal scrapes, and deliver ONE combined, deduped, overlap-flagged CSV when they finish.

**Architecture:** A first-class `scraper_batches` parent + `batch_runs` execution row + nullable `scraper_configs.batch_id`. A batch fans out into N ordinary scraper_configs/jobs (existing scheduler/watchdog/enrichment unchanged) with child delivery/schedule SUPPRESSED. A completion barrier fires when all child jobs are terminal → builds the combined CSV (reusing Piece 1's `write_lead_csv_with_overlap` scoped to the batch's job_ids) → one delivery. Pro+ gated, capped, quota-preflighted.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, Celery/Redis, R2, Resend; Next.js frontend.

**Spec:** `docs/superpowers/specs/2026-06-10-batch-scrape-design.md`. **Depends on:** Piece 1 (merged) — reuses `results.date_recorded_parsed` (not required) and `src/utils/lead_export.write_lead_csv_with_overlap` (required).

**Biggest risk to retire FIRST (Codex):** completion semantics — the `batch_run` state machine, child terminal-state detection, partial failure, and the export barrier.

---

## Phase 2A.0 — Research (read before writing any code)

- [ ] **R1: Config creation + job dispatch.** Read `src/api/routes/scrapers.py` (the create-scraper route) and `src/workers/tasks.py` + `src/workers/scheduler.py` to capture EXACTLY: how a `ScraperConfig` row is created from the wizard payload; how a `Job` is created and enqueued (the Celery task name + signature); how a job reaches a terminal status (`done`/`failed`/`cancelled`) and where that transition is written; the existing `dialer_push_sweep` pattern (the proven "fire after jobs settle" beat). Write findings as comments in this plan's tasks before coding.
- [ ] **R2: Delivery + export.** Read `src/workers/delivery.py` + `src/utils/data_exporter.py`: the exact call to build a CSV via `DataExporter.export()`, upload to R2, and send via Resend. Note the R2 key scheme and the email-send signature.
- [ ] **R3: Billing + plan limits.** Read where `User.records_used`/quota is enforced and where `PLAN_LIMITS` lives (note the pre-existing `PLAN_LIMITS["pro"]=1000` vs register `records_limit=500` drift — do NOT depend on it being consistent; reconcile or read the canonical source). Capture how to check a user's plan + remaining quota.
- [ ] **R4: Frontend submit.** Read `bridgeleads-web/app/(dashboard)/scrapers/new/page.tsx` Step 0 + submit handler to see how the single-scrape payload is built and POSTed, so the Batch fork mirrors it.

Output of 2A.0: fill the `// VERIFIED:` notes in the tasks below. If any assumption here is wrong, STOP and revise the plan.

---

## Phase 2A.1 — Data model (migration + models)

**Files:** Create `alembic/versions/0NN_add_scraper_batches.py` (NN = next free after 049 — confirm head); Modify `src/db/models.py`; Test `tests/test_batch_models.py`.

- [ ] **Step 1: Models.** Add `ScraperBatch` (`id, user_id, name, state, fields JSON, enrichment JSON, schedule JSON, deliver JSON, status, created_at`), `BatchRun` (`id, batch_id, user_id, status, child_job_ids JSON, combined_export_key, excluded_no_date_count, failed_children JSON, created_at, completed_at`), and `ScraperConfig.batch_id` (nullable FK). `BatchRun` is WORKER/SYSTEM-written like `DialerDelivery` — NOT app-table-granted; a read endpoint uses the system session + explicit `user_id` filter (mirror the dialer outbox; no RLS-cutover-script change).
- [ ] **Step 2: Migration** creating both tables + the nullable column + indexes (`scraper_configs.batch_id`, `batch_runs(batch_id)`, per-tenant). Idempotent-safe. Single migration.
- [ ] **Step 3: Pure model test** — table names, `batch_id` nullable, `BatchRun` status default. Run `pytest tests/test_batch_models.py -v` (create_all on test DB) — DB-applied verification deferred to CI like Piece 1.
- [ ] **Step 4: Commit** `feat(batch): scraper_batches + batch_runs model + migration`.
- [ ] **Codex gate** on the migration + models.

---

## Phase 2A.2 — Batch creation API (fan-out, gating, caps, preflight)

**Files:** Create `src/api/routes/batches.py`; add schemas to `src/api/schemas.py`; register router; constants in `src/api/constants.py` (or existing). Test `tests/test_batches_create.py`.

- [ ] **Step 1: Request schema** `BatchCreateRequest` (`name?, state, counties: list[str] (1+), record_types: list[str] (1+), fields, enrichment, schedule?, deliver?`). Validate: counties × record_types resolve to real, available connectors; total combos ≤ the per-plan cap; reject empty.
- [ ] **Step 2: Plan gate + caps + quota preflight.** `POST /batches`: require **Pro+** (Free/Starter → 403); enforce `len(counties) * len(record_types) ≤ PLAN_BATCH_CAP[plan]` (Pro smaller, Business larger — exact from R3); preflight remaining monthly quota vs expected combos (reject/`409` if it can't fit). Hard absolute ceiling regardless of plan.
- [ ] **Step 3: Fan-out (the core).** In one txn: create `ScraperBatch` (holds shared fields/enrichment/schedule/deliver); for each (county, record_type) create a child `ScraperConfig` with `batch_id` set and **delivery + schedule SUPPRESSED** (empty `deliver`, no schedule — the batch owns them); create a `BatchRun(status=pending, child_job_ids=[...])`; enqueue each child job via the SAME path single-scrape uses (from R1). Every query `user_id`-scoped.
- [ ] **Step 4: Tests** (pure where possible): plan gate blocks Free/Starter; cap enforced; combos expand correctly; child configs carry `batch_id` + suppressed delivery. Run `pytest tests/test_batches_create.py -v`.
- [ ] **Step 5: Commit** + **Codex gate** (focus: tenant isolation, cap/quota bypass, child-delivery suppression).

---

## Phase 2A.3 — Completion barrier + combined export + delivery (retire the risk)

**Files:** Modify `src/workers/scheduler.py` (a `batch_completion_sweep` beat, mirroring `dialer_push_sweep`); create `src/workers/batch_export.py`; Test `tests/test_batch_export.py`.

- [ ] **Step 1: Completion barrier.** A beat (or job-completion hook from R1) that, for each `pending`/`running` `batch_run`, checks whether ALL `child_job_ids` are terminal. Durable atomic claim before exporting (at-most-once — mirror the dialer sweep's claim). If some children `failed` → proceed with successes, record `failed_children`, final status `partial`; else `done`.
- [ ] **Step 2: Combined export.** `build_batch_combined_csv(batch_run)`: query `results` scoped to `batch_run.child_job_ids` (NOT all-history), dedup `COALESCE(property_key, dedup_hash, 'id:'||id)`, `overlap_count = count(distinct record_type)` within the batch, aggregate `record_types_present` + `source_counties`; decrypt PII; sort hottest-first; write via `write_lead_csv_with_overlap` (Piece 1) → `DataExporter.export()` → R2 (`combined_export_key`). Do NOT wait for skip-trace (contacts may be partial; re-download later).
- [ ] **Step 3: Delivery.** One combined CSV via the existing Resend/R2 path (from R2); email reflects `done` vs `partial` (+ failed-child summary). Set `batch_run.completed_at`.
- [ ] **Step 4: Tests** — barrier waits for all-terminal; partial-failure path; combined query scoped to batch jobs only (no cross-batch/cross-tenant leakage); dedup + overlap_count within batch. Pure where possible; DB rows in CI.
- [ ] **Step 5: Commit** + **Codex gate** (focus: at-most-once claim, partial handling, scoping, no skip-trace block).

---

## Phase 2A.4 — Batch read + download endpoints

**Files:** `src/api/routes/batches.py` (extend); Test `tests/test_batches_read.py`.

- [x] **Step 1:** `GET /batches` (list user's batches), `GET /batches/{id}` (status + per-child summary + combined_export_key presence), `GET /batches/{id}/download` (signed R2 URL or stream of the combined CSV). System session + explicit `user_id` filter (BatchRun not app-granted). Re-download reflects later skip-trace fills.
  - // DONE `185f04a`: `get_db`-style reads via `get_rls_db` (RLS belt for joined scraper_configs/jobs) + explicit `user_id` on every batch-table query (the only boundary for non-RLS scraper_batches/batch_runs). Download returns a short-lived **presigned R2 URL** (`get_download_url`, the prod S3-presign path) — NOT a stream (`download_object` uses the unconfigured Cloudflare REST API in prod). Raw `combined_export_key` never exposed.
  - // Codex P2: `_run_for` JOINs the owned `ScraperBatch` (don't trust `BatchRun.user_id` alone); P3: `children` uses `default_factory=list`.
- [x] **Step 2: Tests** (tenant isolation: a user can't read another's batch). Commit + **Codex gate**.
  - // DONE `tests/test_batches_read.py`: pure schema/route tests pass locally; DB-backed list/detail/download + tenant-isolation tests run in CI (no local Postgres on this box — same as all route tests).

---

## Phase 2A.5 — Frontend: Single | Batch wizard fork + batch-run view

**Files:** `bridgeleads-web/app/(dashboard)/scrapers/new/page.tsx`; `lib/api.ts`; `lib/types.ts`; new `app/(dashboard)/batches/[id]/page.tsx` (or a section). Mirror R4.

- [x] **Step 1:** Step 0 gains a **Single | Batch** choice after state. Single = unchanged. Batch = county multi-select + record-type multi-select + a live "will run N scrapes (X×Y)" line + cap/quota warning. Steps Fields/Schedule(deferred 2B)/Delivery shared.
  - // DONE `8e884ac` (branch `feature/batch-scrape-ui` off **master**, per user — NOT off the unmerged Piece 1 Lists UI). Body renders by logical `screen` from a `screens` array (batch omits "schedule"), so skipping Schedule needs no per-section index math. Record types = **intersection** across chosen counties (every combo supported → no backend 422). Batch lock is Pro+ UX-only (backend 402 authoritative). Cap is advisory client-side.
- [x] **Step 2:** `createBatch()` in api.ts; types for batch request/response. On submit → POST /batches → redirect to the batch-run view.
  - // DONE `34aa465`: `createBatch`/`listBatches`/`getBatch`/`getBatchDownloadUrl` + `Batch*` types. Batch selections live in plain `useState` (single zod schema would reject them); `<form onSubmit>` branches by mode so the Enter key never runs the single schema in batch.
- [x] **Step 3:** Batch-run view: status (running/done/partial), per-child summary, **Download combined CSV**, "contacts still filling" note. Optionally link to the equivalent Lists view.
  - // DONE `34aa465`: `app/(dashboard)/batches/[id]/page.tsx` polls every 3s until terminal; download via presigned URL (`window.location.href`); partial-failure note.
- [x] **Step 4:** `npx tsc --noEmit && npx next build` clean. Commit (frontend branch, no deploy) + **Codex gate**.
  - // DONE: tsc + `next build` both clean (`/batches/[id]` + `/scrapers/new` compile). Codex-gated: P3 clamp `setStep`, P2 batch button `type=submit`, P2 mode-aware name field.

---

## Phase 2B (later, separate plan) — Scheduled batch
A batch owns a schedule; a scheduled `batch_run` dispatches children together; the completion barrier triggers one combined export + re-delivery. Needs its OWN schedule entity (Codex: don't lean on per-config schedules). Out of scope for 2A.

## Self-Review
- Spec coverage: model (2A.1), fan-out+gating+caps+preflight (2A.2), barrier+export+delivery (2A.3), read/download (2A.4), wizard+view (2A.5), 2B deferred. All §3–§7 of the spec covered for on-demand; §7 scheduler is 2B.
- The completion barrier is sequenced FIRST among the risky work (2A.3) per Codex.
- Reuses Piece 1 `write_lead_csv_with_overlap` (no CSV re-implementation).
- Every batch query is `user_id`-scoped; `BatchRun` is system-written (read via system session + explicit filter).
- ⚠️ Migration number + plan-cap values resolved during 2A.0/2A.1 (not guessed).
