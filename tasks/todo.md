# Batch 2B — Scheduled Batches (Codex-reconciled 2026-06-11)

**Goal:** Recurring batch scrapes on a schedule, completing the Piece-2 batch story. Track A made
everything downstream durable — a scheduled run is "create `BatchRun(pending)` when due."
**Codex verdict on v1 plan: NO-GO** — 4 P1s, all adopted below. This is the reconciled design.

## Design (post-Codex)

- **Scheduler creates the pending run, never the API** (per the reserved comment in
  `batches.py:163-165`). New beat `dispatch_scheduled_batches` every minute.
- **Migration 052** (all three, one migration):
  1. Drop `uq_batch_runs_batch_id`; add **partial unique index**
     `ON batch_runs(batch_id) WHERE status IN ('pending','running')` — at-most-one ACTIVE run.
  2. **Add `scheduled_for` column** (nullable timestamptz; NULL = on-demand run) + unique
     `(batch_id, scheduled_for)` — durable OCCURRENCE idempotency (🔑 Codex P1: the ±1-min
     `_should_run_now` window can double-fire a batch that completes <1min; active-run dedupe
     dies at terminal. Single scrapers have this latent bug too — noted, not fixed here).
  3. Remove the model-level `UniqueConstraint("batch_id")` (else create_all/tests re-enforce it).
- **🔑 Dispatch contract: `dispatch_batch_run(run_id)` not `(batch_id)`** (Codex P1: it currently
  selects BatchRun by batch_id + `scalar_one_or_none()` → MultipleResultsFound with history).
  Recovery re-dispatches RUN ids. Transitional: accept old batch_id payloads for queued
  pre-deploy tasks (resolve via the active run).
- **Scheduler INSERT race** (Codex P1): create the pending run via
  `INSERT ... ON CONFLICT DO NOTHING` against the partial index (or catch IntegrityError) —
  a read-then-insert check alone races on dual beat ticks.
- **Readers → deterministic latest-run** (Codex P1): `_run_for()` = ORDER BY created_at DESC
  LIMIT 1; list summaries = `DISTINCT ON (batch_id) ... ORDER BY batch_id, created_at DESC`.
- **Stuck-child recovery** (Codex P2): drive off the active run's `child_job_ids`, not the
  `Job → ScraperConfig.batch_id → BatchRun` join (brittle once runs are plural).
- **Run-scoped API before frontend history** (Codex P2): `GET /batches/{id}/runs` (history) +
  run-scoped download `GET /batches/{id}/runs/{run_id}/download`; delivery email keeps linking
  the batch page (which shows latest + history).
- **POST /batches accepts optional `schedule`**; children stay suppressed (frequency=manual —
  verified `dispatch_scheduled_jobs` skips them). No schedule = today's behavior exactly.
  With schedule: still runs immediately on create (`scheduled_for=NULL`), recurs when due.

## Phases (each ≤5 files, Codex-gated, user check-in between)

### Phase 1 — backend foundation: plural runs + run-scoped dispatch
- [ ] Migration 052 (partial unique + scheduled_for + occurrence unique; model updates)
- [ ] `dispatch_batch_run` → run_id contract (+ transitional batch_id acceptance); recovery
      re-dispatches run ids; stuck-child recovery via active run's child_job_ids
- [ ] `_run_for()` + list summaries → deterministic latest-run
- [ ] Fold in: recovery give-up path sets `completed_at` (Backlog §6 one-liner)
- [ ] Tests: plural terminal + one active; latest-run reads; dispatch by run_id
- [ ] Codex review gate

### Phase 2 — backend: the schedule fires
- [ ] `dispatch_scheduled_batches` beat (reuse `_should_run_now`; record-limit check;
      occurrence key = due-tick timestamp truncated to minute; ON CONFLICT DO NOTHING)
- [ ] `POST /batches` + schema accept optional validated `schedule`
- [ ] `GET /batches/{id}/runs` + run-scoped download endpoint
- [ ] Tests: due-ness, double-beat race, fast-batch re-fire blocked by occurrence unique,
      record-limit skip
- [ ] Codex review gate

### Phase 3 — frontend: wizard + run history
- [ ] Wizard renders Schedule screen in batch mode; payload carries `schedule`
- [ ] Batch run view: history list (latest first) wired to /runs; download per run
- [ ] `tsc --noEmit` + Codex review gate

## Confirmed safe (Codex-verified)
- Completion sweep claims/finalizes by `BatchRun.id` — plural-safe. ✓
- Combined-CSV R2 key is per-run (`batch/{run.id}/combined.csv`). ✓
- Billing is per child job — no one-run assumption. ✓

## Review
_(filled at the end)_
