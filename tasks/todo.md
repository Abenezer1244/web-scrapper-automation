# Batch Scrape — Crash-Durability Hardening (Track A)

**Goal:** Close the 3 crash windows in the batch-scrape flow that can strand a batch forever.
Design reconciled with Codex (consult 2026-06-11) — Codex rejected the original "no-migration"
sketch; durable recovery needs durable state. Each phase ≤5 files, Codex-gated before the next.

## Design (Codex-reconciled)

- **BatchRun is the single durable object.** It is created `status='pending'` in the **API
  transaction** (same txn as `ScraperBatch` + child `ScraperConfig`s), NOT by the worker. This
  makes dispatch intent durable (kills Gap 1 at the root) and future-proofs Phase 2B (scheduled
  batches create a `pending` run when their schedule fires). Worker `dispatch_batch_run`
  transitions `pending→running` + creates child jobs (idempotent via `UNIQUE(batch_id)`).
- **`claimed_at` becomes a real lease** (30-min TTL > worst-case finalize) + `claim_token` for
  unambiguous ownership — fixes Gap 2 (hard-kill mid-finalize stranding `running` forever).
- **`delivery_started_at` CAS** makes the combined-CSV email at-most-once (status-guard protects
  DB state, not the post-commit best-effort email — Codex obj #3).
- **`dispatch_attempts`** bounds re-dispatch/re-enqueue so a poisoned job or broker-down can't
  storm forever (Codex obj #5).
- **One claim/finalize path:** the existing `batch_completion_sweep` owns BOTH normal completion
  (all children terminal) AND force-finalize (age > hard deadline) — same lease/CAS, one
  `finalize_batch_run(mode)` (Codex obj #4). No separate beat for the backstop.
- **New `batch_recovery_sweep` beat** owns only pre-finalize recovery: re-dispatch `pending`
  runs (Gap 1) + re-`.delay()` pending children (Gap 3a), both bounded by `dispatch_attempts`.
- **Prerequisite (pre-existing bug):** `run_scrape_job` `pending→queued` is a BLIND set
  (`tasks.py:384`), not an atomic CAS. The recovery's safety REQUIRES a guarded claim
  (`UPDATE ... WHERE status='pending'`, exit on rowcount 0) or re-enqueue double-scrapes
  (Codex obj #8).

## Phases

### Phase 1 — Durable state migration + model  ☐
- [ ] `alembic/versions/051_batch_durability.py` — add to `batch_runs`: `dispatch_attempts INT
      NOT NULL DEFAULT 0`, `delivery_started_at TIMESTAMPTZ NULL`, `claim_token VARCHAR(36) NULL`.
      (Reuse existing `status='pending'` as intent — already the model default + documented
      lifecycle.) down_revision="050".
- [ ] `src/db/models.py` — add the 3 columns to `BatchRun`.
- [ ] Verify: `py_compile` + `ruff` + model imports. Codex-gate.

### Phase 2 — Atomic pending→queued claim (the prerequisite)  ☐
- [ ] `src/workers/tasks.py` — replace the blind `_set_status(job,"queued")` with an atomic
      `UPDATE jobs SET status='queued', started_at=now() WHERE id=:id AND status='pending'`
      (rowcount 0 ⇒ another worker owns it / not pending ⇒ return). Keep the cancelled checks.
- [ ] `tests/` — regression: two deliveries of one job ⇒ exactly one scrape.
- [ ] Verify + Codex-gate. (This is also Backlog §5 "atomic claim" — closes it.)

### Phase 3 — BatchRun-as-intent  ☐
- [ ] `src/api/routes/batches.py` — create `BatchRun(status='pending', child_job_ids=[])` in the
      same txn as the batch+configs; still `dispatch_batch_run.delay(batch.id)` after commit.
- [ ] `src/workers/batch_tasks.py` — `dispatch_batch_run` now LOADS the pending run (created by
      API) and transitions `pending→running` + creates jobs; the over-limit/empty terminal paths
      move onto the existing run; keep `UNIQUE(batch_id)` idempotency + recovery branch.
- [ ] Update `tests/test_batches.py` / `test_batch_models.py`.
- [ ] Verify + Codex-gate.

### Phase 4 — Lease + delivery idempotency  ☐
- [ ] `src/workers/scheduler.py` (`batch_completion_sweep`) — select + CAS claim become a LEASE
      (`claimed_at IS NULL OR claimed_at < now()-30min`) with a generated `claim_token`.
- [ ] `src/workers/batch_export.py` (`finalize_batch_run`) — gate the delivery email on a CAS
      `UPDATE ... SET delivery_started_at=now() WHERE id=:id AND delivery_started_at IS NULL`.
- [ ] Tests.  Verify + Codex-gate.

### Phase 5 — Recovery sweep + force-finalize  ☐
- [ ] `src/workers/scheduler.py` — new `batch_recovery_sweep` beat: (a) `pending` runs older than
      3 min ⇒ re-`dispatch_batch_run.delay` (bounded by `dispatch_attempts`); (b) `running` runs
      ⇒ re-`.delay()` pending children (bounded). Register in `beat_schedule`.
- [ ] `src/workers/scheduler.py` (`batch_completion_sweep`) + `batch_export.py` — add force-finalize
      eligibility (age > 90 min ⇒ treat missing/non-terminal children as failed, build CSV from
      completed, mark partial/failed) through the SAME claim/finalize path.
- [ ] Tests.  Verify + Codex-gate.

### Phase 6 — Final review + docs  ☐
- [ ] Full Codex review of the whole diff (`codex challenge` adversarial).
- [ ] Security Master Review (§14) — touches workers + an endpoint.
- [ ] `docs/BUILD_JOURNAL.md` entry + `tasks/BACKLOG.md` update (mark durability done; note the
      H1 follow-up: add `batch_runs` to app-role grants in `provision_rls_roles.sql`).

## Notes / risks
- Migrations run via `scripts/migrate.py` (advisory lock), NOT bare `alembic upgrade head`.
- Do NOT apply 051 to prod DB until merged to main (branch-only migration = api crash-loop landmine).
- API now writes `batch_runs` from the rls async session — OK today (RLS_ENFORCE=False, BYPASSRLS
  prod role). H1 cutover MUST add `batch_runs` INSERT grant for the app role (logged in backlog).
- Branch off `main`: `feature/batch-durability`.

## Review
_(to be filled at the end)_
