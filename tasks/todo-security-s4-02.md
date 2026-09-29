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

## Design (proposed, before the Codex consult)

Make liveness observable after a cancel, and release the slot on positive evidence
that the worker has stopped:

1. **Keep beating a cancelled-but-running attempt.** `_HEARTBEAT_SQL` excludes only
   `done`/`failed`. A cancelled row's `last_heartbeat_at` then keeps moving while the
   worker process lives. The write stays attempt-scoped (`started_at = :sa`), so it
   cannot touch another attempt.
2. **Exit acknowledgement.** When the task body exits (`HeartbeatThread.stop()` from
   `__exit__`, which covers return, exception and soft timeout), after the thread is
   joined: `UPDATE jobs SET last_heartbeat_at = NULL WHERE id=:j AND started_at=:sa
   AND status='cancelled'`. NULL on a started cancelled row means "the worker said it
   stopped". Every claim stamps `last_heartbeat_at`, so a started row has NULL only
   after this acknowledgement.
3. **Predicate.** A cancelled job holds the slot while
   `started_at IS NOT NULL AND last_heartbeat_at > now - HEARTBEAT_STALE_MINUTES`
   (15 min, the constant the watchdog and the UI already share). A hard-killed worker
   (deploy, OOM, hard limit) stops beating and releases the slot after at most 15
   minutes of silence. A clean exit releases it immediately.
   `RUN_SLOT_CANCEL_COOLDOWN_SECONDS` goes away.

No migration. There are no pre-deploy stragglers: a deploy restarts the worker, and
old cancelled rows carry their last beat, so they drop out within 15 minutes.

### Alternatives considered

- **New `worker_stopped_at` column (migration).** Cleaner semantics, but it needs a
  migration (quiesce and deploy risk). Without liveness it also has to fall back to
  the 65 min ceiling after a crash. Rejected unless Codex objects to the NULL overload.
- **A longer fixed cooldown (65 min = the hard limit).** Correct but a bad UX: every
  cancel blocks the scraper for an hour.
- **Keep the 300 s floor AND the heartbeat.** Strictly no weaker than today even if
  heartbeats fail; costs the fast release. Open question for Codex.

## Open questions for Codex

- Is overloading `last_heartbeat_at = NULL` as the acknowledgement safe? Does any
  reader treat NULL on a cancelled row differently?
- Should the window be 15 min (shared constant) or shorter for cancelled rows?
- Race: `stop()` joins with a 2 s timeout. A heartbeat write in flight after the
  acknowledgement re-stamps the row, so the slot is held up to 15 min (safe
  direction). Acceptable?
- Can anything keep writing claims after the task body exits (threads, pools)?

## Steps

- [ ] 1. Consult Codex on the design (inline, `codex exec -s read-only`); reconcile.
- [ ] 2. Regression test (real `HeartbeatThread`, real SQL): claim a job, start the
      heartbeat, cancel it, move `finished_at` back 301 s, and assert the slot is
      still held while the thread is alive. **Prove it FAILS on main.**
- [ ] 3. Tests: a clean exit releases the slot at once; a silent worker (stale beat)
      releases it; a stale attempt's thread cannot refresh.
- [ ] 4. Implement 1-3 (`status.py`, `models.py`, the comment in `routes/jobs.py`).
      Update the existing cancelled-slot fixtures in `test_config_eligibility.py`
      (they carry no heartbeat).
- [ ] 5. Run related suites on a dedicated `_test` DB: `test_workers`,
      `test_config_eligibility`, the jobs route tests, and the scheduler/batch
      dispatch tests.
- [ ] 6. Codex diff review until GATE: PASS. Record each round.
- [ ] 7. Review section; no push/merge without the owner's OK.

## Review

(pending)
