# Batch Scrape — Design

**Date:** 2026-06-10
**Status:** Spec — approved for planning (Piece 2 of 2; depends on `2026-06-10-lists-date-window-overlap-foundation-design.md`)
**Author:** Claude (brainstormed with user) + Codex consults (sessions 019eb34c, 019eb357)
**Reviewers:** Codex consult complete; Codex diff review required per phase before merge.

---

## 1. Problem / Motivation

Today a scrape pulls **one county + one record type**. The user wants to pull **several lists at once** and get **one combined, deduped CSV** with overlap baked in. Verbatim intent:

> "a user click on new run then choose state then … select a single scrap or batch. … batch … the user can have the option to select counties and record types as well and then select the field to collect, enrichment, schedule and the delivery and then run … the user will receive a downloadable csv of batch data."

This is the **acquisition** half of the user's goal (spend quota to pull new data → combined CSV). The **free historical** half lives on the Lists page (Piece 1). Both are kept; they are not redundant (Codex + Claude, session 019eb357): batch answers *"what did I scrape in this run?"*, Lists answers *"across everything I already own, who overlaps?"*

### User decisions (locked 2026-06-10)
- Wizard **forks after choosing state**: **Single** (today's journey, unchanged) vs **Batch** (multi-county × multi-record-type).
- Batch shares the same downstream steps (fields, enrichment, schedule, delivery).
- Output = **one downloadable combined CSV** of the batch's data, deduped, with overlap inline.
- **Plan gating: Pro and up, with caps; Business gets bigger caps** (Codex rec, Claude agrees — batch is core productivity, naturally bounded by quota; differs from dialer-webhook Business+ gating). Free/Starter stay single-only.

---

## 2. Current-State Facts (verified by reading code, 2026-06-10)

- **Wizard** `bridgeleads-web/app/(dashboard)/scrapers/new/page.tsx` — 4 steps `["County", "Fields", "Schedule", "Delivery"]`. Step 0 picks state → one connector (county) + one `record_type`. Submits **one** `ScraperConfig`.
- **`ScraperConfig`** = one county + state + record_type; JSON cols `fields`, `enrichment`, `schedule`, `deliver`, `skip_trace_enabled`. The scheduler/watchdog/canary/Celery job pipeline all operate per-config. **No batch grouping today.**
- **`DataExporter.export()`** is the single CSV/JSON/Excel entry point (`.claude/rules/exports.md` — do not bypass); R2 upload + Resend delivery already exist per job (`src/workers/delivery.py`, `src/utils/data_exporter.py`).
- **Union/combine logic already exists** (`src/api/routes/segments.py` `_UNION_SQL`): merge results across record_types, dedup `COALESCE(property_key, dedup_hash, 'id:'||id)`, `overlap_count = count(distinct record_type)`. Piece 1 makes a date-scoped, results-based variant of this — **batch reuses it scoped to the batch's job_ids.**
- **Billing** (`src/workers/tasks.py`): billable count increments `User.records_used` at scrape time, per job.
- **Skip-trace async** (sweep after job done).

---

## 3. Data Model (Codex [P1]: small first-class batch parent, not just a tag)

Two new tables; one nullable column on the existing config table. Single scrapes are completely unaffected (`batch_id = NULL`).

- **`scraper_batches`** — the batch definition (the parent):
  `id, user_id, name, state, fields (JSON), enrichment (JSON), schedule (JSON), deliver (JSON), status, created_at`.
  Holds the **shared** settings so children never drift (Codex trap).
- **`scraper_configs.batch_id`** — nullable FK → `scraper_batches.id`. A batch = N child configs (one per county × record_type).
- **`batch_runs`** — one execution of a batch:
  `id, batch_id, user_id, status (pending|running|done|partial|failed|cancelled), child_job_ids (JSON), combined_export_key (R2), excluded_no_date_count, created_at, completed_at`.
  Per-tenant RLS belt; **worker/system-written** like `delivered_records`/`dialer_deliveries` — NOT app-granted; any read endpoint uses the system session + explicit `user_id` filter (no RLS-cutover-script change; mirrors `project_lead_targeting_milestone` dialer outbox).

Migration: one migration adds all three changes; **next free number** (confirm at implementation, renumber on rebase per `incident_migration_branch_mismatch`).

---

## 4. Execution Flow

### 4.1 Fan-out (reuse the whole existing pipeline — Codex [P1])
On batch run, create **N ordinary child `ScraperConfig` jobs** (counties × record_types), each carrying `batch_id` and the shared fields/enrichment. They flow through the **existing** scheduler/watchdog/canary/dedup/enrichment unchanged.

**Traps to enforce (Codex):**
- Child configs must **NOT send their own delivery emails** — the batch owns delivery. (Children created with delivery suppressed / `deliver` empty; batch holds the real `deliver`.)
- Child configs must **NOT run their own schedules** — the batch owns scheduling (Phase 3).
- Shared settings come from the parent; children never independently mutate them.
- Every child query stays `user_id`-scoped.

### 4.2 Completion barrier (Codex [P1] — the biggest risk, retire first)
A `batch_run` reaches a terminal export only when **all child jobs reach a terminal state** (`done`/`failed`/`cancelled`) AND their `property_key` enrichment is written. Model explicitly:
- child terminal-state detection (poll or event on job completion — reuse the dialer-sweep pattern that already fires after jobs settle, `src/workers/scheduler.py`);
- partial failure: some children fail → still proceed with successes, mark run `partial`, record failed-child summaries in `batch_runs` metadata + the delivery email (Codex [P1/P5]).
- **Do NOT wait for skip-trace** (Codex [P1] option a): generate the CSV when children are enriched; contacts may be partial; allow **re-download / optional re-deliver** after the skip-trace sweep fills phone/email.

### 4.3 Combined export (reuse Piece 1's results-based union, scoped to batch jobs)
Compute the combined set from `results` **scoped to `batch_run.child_job_ids`** (NOT all-history-by-record_type):
- dedup `COALESCE(property_key, dedup_hash, 'id:'||id)`;
- `overlap_count = count(distinct record_type)` **within the batch**;
- aggregate `record_types_present` + `source_counties` per deduped property (Codex).

Note: within a batch, overlap IS meaningful (the batch contains multiple record types by design) — this is what dissolved the earlier "one job = one type, so overlap impossible in a run" tension.

### 4.4 Combined CSV format — organized, dialer-ready, NOT a dump (user decisions 2026-06-10)
**Reuse the canonical builder, do not hand-roll.** `src/utils/lead_export.py` already produces the dialer-ready format: owner split into `first_name`/`last_name` (entities/estates handled), property split into `property_street`/`city`/`state`/`zip` (validated, never corrupting), phones normalized to bare 10-digit, multi-contact `phone_2/3`+`email_2/3`, tax fields, all `sanitize_for_csv`'d, full `party_name`/`property_address` kept as fallback. The combined export **extends** this builder with overlap columns and routes the file through **`DataExporter.export()`** → R2 (`combined_export_key`); never bypass either (`.claude/rules/exports.md`).

**Column order — "caller-first" (regrouped, most actionable left):**
```
overlap, lists_count, lists, counties,
first_name, last_name, phone, phone_type, email, phone_2, phone_3, email_2, email_3,
property_street, property_city, property_state, property_zip,
filed_date, doc_type, delinquent_amount, delinquent_bill_year,
party_name, mailing_address, parcel_id, heirs, legal_description, property_address
```
- `overlap` = the word **"Overlap"** when `lists_count >= 2`, else **blank** (user decision 2026-06-10 — reads/scans better than TRUE/FALSE, still filterable on the word in Excel). The hot multi-list leads visually stand out; single-list rows are empty.
- `lists_count` = number of distinct record types this property is on **within the batch**.
- `lists` = **human-readable** labels joined by "; " (e.g. `Probate; Pre-Foreclosure`), NOT raw tokens — map via the same label table the frontend uses.
- `counties` = `source_counties`, "; "-joined.
- `filed_date` = the county filing date (`date_recorded`), `MM/DD/YYYY`.

**Row sort — "hottest first":** `lists_count` DESC → contactable (has phone OR email) → most recent `date_recorded_parsed` (filing date from Piece 1's column) → id. Best leads at the top.

**Formatting rules:** empty cells are blank (never "None"/"null"); numerics rendered plainly; one header row, no footer/disclaimer rows (the DNC/TCPA notice lives in the delivery email + download UI, per `lead_export.py`).

**Implementation note:** extend `lead_export.py` with an optional overlap-columns variant (e.g. `build_lead_export_row(record, *, overlap_fields=None)` + an extended column list) rather than a parallel writer, so the dialer-ready semantics can never drift between per-job and combined exports.

### 4.5 Delivery
One combined CSV via the **existing** Resend/R2 path. Email reflects `done` vs `partial` (with failed-child summary). Downloadable from the batch-run view; re-downloadable after skip-trace settles.

---

## 5. Billing, Plan Gating, Caps (Codex [P1] — cost/DoS guardrails)

- **Quota:** N child scrapes count as N against the monthly records quota (combine/export itself is free).
- **Plan gate:** **Pro+ creates batches.** Free/Starter = single-only.
- **Per-plan caps** on `counties × record_types` per batch (e.g. Pro 10–25 child scrapes, Business 50–100 — exact numbers confirmed in plan from `PLAN_LIMITS`). Note pre-existing `PLAN_LIMITS["pro"]` vs register `records_limit` mismatch flagged in `project_audit_m3_m8` — reconcile when touching limits.
- **Quota preflight** before enqueue: reject/῾warn if the batch would exceed remaining monthly quota (don't enqueue 120 scrapes that will half-fail at the quota wall).
- **Hard API validation cap** regardless of plan (absolute ceiling) + **batch-level cancellation** (cancel all pending children).
- Concurrency stays Celery-controlled (existing).

---

## 6. Phasing (each ships + Codex-reviewed independently)

| Phase | Goal | Risk |
|---|---|---|
| **2A** | Batch model (`scraper_batches`, `batch_runs`, `batch_id`) + on-demand batch: wizard fork, fan-out, completion barrier, combined CSV via DataExporter, one delivery, Pro+ gate + caps + preflight + cancel. | **Med-High** (completion semantics) |
| **2B** | Scheduled batch: batch owns a schedule; a scheduled `batch_run` dispatches children together; completion barrier triggers one combined export + re-delivery. **Batch needs its OWN schedule entity** (Codex [P1]) — do not rely on per-config schedules. | Med |

**MVP (Phase 2A), Codex's lean list:** batch parent + run → wizard creates children (no child delivery) → barrier waits for all children terminal → combined CSV from successful jobs only → mark `done`/`partial` → R2 + one email → skip-trace updates appear on later re-download.

---

## 7. UI (`bridgeleads-web`)
- Wizard step 0 gains a **Single | Batch** choice (after state). Single = unchanged path.
- Batch: county **multi-select** + record-type **multi-select** (intersect availability per connector); a live "→ will run N scrapes (X counties × Y types)" line + cap/quota warning.
- Steps Fields / Schedule / Delivery shared (Schedule deferred to 2B — 2A is on-demand).
- A **batch-run view**: status (running/done/partial), per-child summary, **Download combined CSV**, re-download note for pending contacts. Optionally link to the equivalent Lists view (Codex: batch may link into Lists for historical lookback).

## 8. Testing
- Fan-out creates exactly N children with `batch_id`, shared settings, delivery suppressed.
- Completion barrier: all-terminal detection; partial-failure → `partial` + successes exported; cancellation.
- Combined export scoped to batch job_ids only (no cross-batch / cross-tenant leakage); dedup + `overlap_count` within batch; routed through DataExporter + sanitize_for_csv.
- Quota preflight rejects over-quota batches; plan gate blocks Free/Starter; caps enforced.
- Re-download after skip-trace settle reflects new contacts.
- No-mock, real-DB (`.claude/rules/testing.md`); tenant isolation on every batch query.

## 9. Risks
- **[P1] Completion semantics** (batch-run state machine + child terminal states + partial failure + export barrier) — retire FIRST; everything (delivery, billing, retries, trust) depends on it.
- **[P1] Cost/DoS** from large batches — caps + quota preflight + hard ceiling + cancel.
- **[P2] Child delivery/schedule leakage** — children must not self-deliver or self-schedule.
- **[P2] Skip-trace partiality** — "list ready" ≠ "contacts complete"; surface clearly + re-download.
- **[P2] Migration/branch** — merge before prod; advisory-lock migrate.

## 10. Out of scope
- The Lists date-window / historical overlap (Piece 1 — separate spec; this reuses its query engine).
- Native per-dialer batch push (existing dialer feature unchanged).
- Non-CSV batch export formats (CSV first).
