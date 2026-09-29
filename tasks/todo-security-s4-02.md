# S4-02 (P2): a cancelled run's slot outlives a fixed 300 s cooldown

Branch `fix/security-s4-02-run-slot-cooldown` off `origin/main` `6675a60a`, worktree
`C:/Users/Windows/bl-wt-s402`.

## Finding

`Job.holds_run_slot(now)` (`src/db/models.py`) treats a cancelled job whose worker had
started as occupying its scraper's run slot only while
`finished_at > now - RUN_SLOT_CANCEL_COOLDOWN_SECONDS` (300 s). After a cancel the
worker keeps going until it hits a cancel check, and nothing bounds that by 300 s
(the task's hard `time_limit` is 3900 s). If a new run starts in between, the old
worker's dedup claims (`delivered_records.first_job_id`) can make the new run hide
leads as "already delivered". Liveness cannot be read today because the heartbeat
CAS (`_HEARTBEAT_SQL`) excludes `cancelled`, so the thread self-reaps at the cancel.

Every start path goes through `holds_run_slot`: POST /jobs (`routes/jobs.py:200`),
config eligibility (`config_eligibility.py:149`), the scheduler
(`scheduler_helpers/dispatch.py:52`) and the batch fan-out (`batch_tasks.py:235`).
Fixing the predicate therefore fixes every path.

## Design (as built, after two Codex consults)

The design first proposed (keep beating cancelled rows; release on a stale beat)
was rejected by Codex consult r1: heartbeat writes are best-effort, so a heartbeat
outage would release the slot under a live worker. As built:

1. **Exit acknowledgement is authoritative.** `HeartbeatThread.__exit__` (the
   outermost `with` of `run_scrape_job`, after the work session closed) runs
   `_acknowledge_exit`: `UPDATE jobs SET last_heartbeat_at = NULL WHERE id AND
   started_at = <this attempt> AND status = 'cancelled'`. It is attempt-scoped and
   touches only cancelled rows. Errors are logged, never raised (Celery time limits
   are re-raised per `reraise_time_limit`).
2. **Otherwise the hard limit.** A cancelled, unacknowledged attempt holds the slot
   until `started_at + RUN_SCRAPE_TIME_LIMIT_S + 120 s` (`Job.RUN_SLOT_RELEASE_AFTER_S`).
   Prefork kills the child at the hard limit, and the claim runs inside the task.
3. **One clock.** The claim stamps `started_at`/`last_heartbeat_at` with the DB's
   `now()`, and `holds_run_slot()` judges age by the DB's `now()` (consult r2 P1,
   clock skew). The `now` parameter is gone from all four call sites.
4. **The heartbeat starts right after the claim**, before the gates that can
   return, so every exit of a claimed attempt acknowledges.
5. The task decorator reads `RUN_SCRAPE_(SOFT_)TIME_LIMIT_S` from constants.
6. The heartbeat SQL is unchanged (it still never writes a cancelled row), so a
   late beat cannot undo the acknowledgement.

Rejected (consult r2 P1): "the watchdog re-queue must be fenced". The watchdog
selects only active statuses, and `_recovery_cas` already CASes status +
started_at + retry_count.

## Steps

- [x] 1. Codex design consult: r1 FAIL (heartbeat-outage hole, adopted), r2 FAIL
      (clock skew adopted; watchdog fencing rejected with evidence).
- [x] 2. Regression test, proven to FAIL on main: 6 min after a cancel, with the
      worker unacknowledged, `POST /jobs` returned 201 (a second run). The claim's
      started_at was 1.5 s after its transaction's `now()` (Python clock).
- [x] 3. Release tests: ack frees at once; hard-limit ceiling; never-claimed;
      finished job untouched; superseded attempt cannot ack; ack failure never
      masks the task's exception; DB-clock claim; the real task acknowledges on an
      early-gate exit (FAILS with main's `tasks.py`).
- [x] 4. Implemented (8 src files). Fixtures updated in `test_config_eligibility`
      and `test_run_in_flight_guard` (their started cancelled rows had no claim stamp).
- [x] 5. Suites on `bridgeleads_s402_test` (Redis 13): 240 passed (start paths,
      workers, batches) + 607 passed / 36 skipped (billing, dedup, run_scrape_job
      users; skips are missing local Stripe prices). ruff 0.15.6 clean. No
      type-checker is configured.
- [x] 6. Codex diff review: r1 FAIL (P1 legacy NULL-heartbeat rows: rejected with
      evidence; P2 test honesty: adopted), r2 **GATE: PASS**.
- [ ] 7. Push / PR / merge: waiting for the owner's OK.

## Review

- **Behaviour change the owner should know:** a cancelled run now frees its scraper
  the moment the worker stops (usually faster than the old 300 s). If the worker
  dies without stopping cleanly (a deploy or OOM mid-cancel), the scraper stays
  blocked for up to ~67 min after that run STARTED, not 5 min after the cancel.
  The UI copy ("still stopping, try again in a few minutes") is unchanged.
- **At deploy:** any run cancelled shortly before the deploy has no
  acknowledgement, so its scraper stays blocked until its started_at + 67 min.
  This is a one-time cost.
- No migration. The claim now uses the DB clock, which also makes the watchdog's
  staleness math consistent (heartbeats were already DB-clock).
