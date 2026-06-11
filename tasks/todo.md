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

### Phase 1 — Durable state migration + model  ✅ (commit d54f07e)
- [x] `alembic/versions/051_batch_durability.py` — added `dispatch_attempts`, `delivery_started_at`,
      `claim_token` to `batch_runs`. All additive/defaulted, rolling-deploy safe. down_revision="050".
- [x] `src/db/models.py` — 3 columns added to `BatchRun`.
- [x] Verified: py_compile + ruff clean + model import shows the columns.

### Phase 2 — Atomic pending→queued claim (the prerequisite)  ✅ (commits e?/P2 doc)
- [x] `src/workers/tasks.py` — blind `_set_status(job,"queued")` → atomic CAS
      `UPDATE jobs ... WHERE id=:id AND status='pending'` (rowcount 0 ⇒ return). Closes Backlog §5.
- [x] `tests/test_workers.py` — 2 regression tests (at-most-once claim; cancelled job skipped). PASS.
- [x] Codex-gated: GATE PASS, 1×P2 (acks_late recovery tradeoff) accepted + documented.

### Phase 3 — BatchRun-as-intent  ✅ (commit Phase 3 + watchdog fix)
- [x] `src/api/routes/batches.py` — creates `BatchRun(status='pending')` in the same txn (durable intent).
- [x] `src/workers/batch_tasks.py` — `dispatch_batch_run` locks the run FOR UPDATE, transitions
      pending→running + creates jobs (concurrent dispatches serialize); back-compat creates run if
      none; bumps `dispatch_attempts`. Recovery branch unchanged.
- [x] `tests/test_batch_dispatch.py` (new, DB-backed: transition, idempotency, over-limit) +
      `test_batch_models.py` durability-cols test. 8/8 pass (broker-rate-limit tolerated hermetically).
- [x] Codex-gated: GATE PASS, 1×P2 — a REAL bug it caught: watchdog set pending + .delay BEFORE its
      commit; the new CAS would strand the retry. FIXED (commit-before-delay in `scheduler.py`).
- [x] Migration 051 applied to local DB.

> NOTE for Phase 6 journal: residual — a broker outage at the watchdog moment leaves a non-batch job
> committed 'pending' that the next watchdog won't re-pick (excludes pending). Pre-existing property of
> the commit-before-delay convention; batch children are covered by the Phase-5 recovery sweep.

### Phase 4 — Lease + delivery idempotency  ☐
- [ ] `src/workers/scheduler.py` (`batch_completion_sweep`) — select + CAS claim become a LEASE
      (`claimed_at IS NULL OR claimed_at < now()-30min`) with a generated `claim_token`.
- [ ] `src/workers/batch_export.py` (`finalize_batch_run`) — gate the delivery email on a CAS
      `UPDATE ... SET delivery_started_at=now() WHERE id=:id AND delivery_started_at IS NULL`.
- [ ] Tests.  Verify + Codex-gate.

### Phase 4 — Lease + delivery idempotency  ✅
- [x] `batch_completion_sweep` claim → reclaimable LEASE (`claimed_at` OR < 30min) + `claim_token`.
- [x] `_deliver` gated on a `delivery_started_at` CAS (at-most-once email).
- [x] Tests + Codex-gate.

### Phase 5 — Recovery sweep + force-finalize  ✅
- [x] `batch_recovery_sweep` beat (every 2min): re-dispatch lost `pending` runs (Gap 1); re-enqueue
      stuck `pending` batch children driven off the JOBS via job→config.batch_id→run (scalable).
- [x] Force-finalize folded into `batch_completion_sweep` (eligibility age>90min from `running_at`),
      one claim/finalize path; `finalize_batch_run(forced=True)` cancels still-active children.
- [x] Failure is TIME-based only (90min), not attempt-based; status-guarded give-up.
- [x] Tests + Codex-gate.

### Phase 6 — Final review + docs  ◑ (in progress)
- [x] Iterative Codex review of the whole diff — ~12 findings across 9 rounds (2 P1 + P2s), all fixed.
- [x] Round 10 (final-gate pass): 2 P2s. (a) `batch_runs` INSERT from the API rls session under split
      roles — ACCEPTED, already the documented H1 follow-up in BACKLOG §2. (b) REAL: force-cancelled
      child could resurrect to `done` + bill — fixed `9e4ad2d`: `_set_status` is now a terminal-write
      CAS (returns False on terminal rows), all transitions guarded, billing re-checks live status
      after export. 2 regression tests; 18/18 worker tests pass. Residual (documented): cancel landing
      between billing-check and done-CAS bills genuinely-scraped records but never resurrects the job;
      a child cancelled post-insert can leave its already-saved rows in the forced CSV unbilled.
- [x] Final gate (round 11): **PASS — no P1s.** 2 P2s, both ACCEPTED design tradeoffs of the
      90-min hard-deadline backstop (and both made safe by `9e4ad2d`):
      (a) pending-only children force-fail at 90min — deliberate: 90min unpicked ≈ 45 failed
      recovery re-enqueues = systemic outage; clear "timed out" beats an infinite spinner.
      (b) force-cancel includes `enriching` children — required by earlier round (664056c, late
      side-effects after terminal batch); terminal-write guard means no resurrect/no email, and
      post-insert rows still reach the user via the forced CSV. Codex is critiquing its own
      prior prescriptions at this point — iteration stopped.
- [x] `docs/BUILD_JOURNAL.md` entry + `tasks/BACKLOG.md` (H1 `batch_runs` grant follow-up logged ✅).

## Notes / risks
- Migrations run via `scripts/migrate.py` (advisory lock), NOT bare `alembic upgrade head`.
- Do NOT apply 051 to prod DB until merged to main (branch-only migration = api crash-loop landmine).
- API now writes `batch_runs` from the rls async session — OK today (RLS_ENFORCE=False, BYPASSRLS
  prod role). H1 cutover MUST add `batch_runs` INSERT grant for the app role (logged in backlog).
- Branch off `main`: `feature/batch-durability`.

## Review

**Track A complete — 16 commits on `feature/batch-durability`, merge-ready.** All 3 crash
windows closed, plus a pre-existing double-scrape bug and a force-cancel resurrection bug:

- **Gap 1 (lost dispatch):** `BatchRun(status='pending')` is created in the API transaction —
  dispatch intent is durable; `batch_recovery_sweep` (2min beat) re-dispatches lost pending runs
  and re-enqueues stuck pending children, bounded by `dispatch_attempts`.
- **Gap 2 (hard-kill mid-finalize):** completion-sweep claim is a reclaimable 30-min lease
  (`claimed_at` + `claim_token`); the delivery email is at-most-once via `delivery_started_at` CAS.
- **Gap 3 (stranded forever):** force-finalize folded into `batch_completion_sweep` at >90min
  from `running_at` — one claim/finalize path; cancels still-active children in the same txn.
- **Prerequisite fix:** `run_scrape_job` pending→queued is an atomic CAS (no double-scrape).
- **Final-gate fix (`9e4ad2d`):** `_set_status` = terminal-write CAS — a force-cancelled child
  can never resurrect to `done`/bill/email; billing re-checks live status post-export.

**Verification:** 26 worker/batch tests pass (incl. 4 new CAS regressions); ruff clean; ~14
Codex findings over 11 review rounds all fixed or explicitly accepted+documented; final gate
PASS (no P1s; 2 accepted-tradeoff P2s, see Phase 6).

**Deploy notes:** migration 051 is additive/rolling-safe but MUST NOT touch prod until this
merges to main (crash-loop landmine). H1 cutover needs a `batch_runs` INSERT grant (BACKLOG §2).
