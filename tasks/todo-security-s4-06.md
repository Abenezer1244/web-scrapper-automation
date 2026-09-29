# S4-06 (P2): quota reservation reads the clock before the users-row lock

**Finding (audit #4 S4-06, re-confirmed audit #5 as CX5-06):** in `run_scrape_job`
the reservation reads `_reserved_at = clock_timestamp()` BEFORE it holds the users
row. The grant evaluates the entitlement window (`window_cte_sql`, lazy rollover)
at that `:at`. A reservation that waits on the users lock across `quota_period_end`
is granted against the OLD window (old counter, old limit) and records the old
window on the job.

**Base:** `origin/main` `bdbbc762` (includes #374, which added
`account_charge_block(..., lock="FOR UPDATE")` inside the reservation: the lock is
already there, only the clock is early). Branch `fix/security-s4-06-reservation-clock`,
worktree `C:/Users/Windows/bl-wt-s406`.

## Codex design consult (2026-09-28): GATE PASS, P2s folded in
- ONE clock read, taken after the users lock, shared by the charge decision, the
  window grant and `jobs.reserved_at` (not two reads microseconds apart).
- Boundary test must be deterministic: boundary from the DB clock; poll `pg_locks`
  to prove B is WAITING on the lock before the boundary; commit A only once the DB
  clock is past it; always release A in cleanup.
- Assert `jobs.quota_period_start IS NOT NULL` for every new reservation (overwriting
  `reserved_at` relies on it: `reservation_is_current_sql` reads `reserved_at` only
  when that column is NULL, pre-088 legacy).
- Old tests: `-1` sentinel becomes `None`; fixtures must be LIVE accounts (the real
  function applies `account_charge_block`, the SQL copy did not); the two `at=`
  tests move the user's window + the job's window/reserved_at back in time instead
  of a test-only clock parameter.
- Out of scope (noted, pre-existing S4-01 limit #374 documents): an UNLIMITED
  account's live pre-read skips the locked re-check.

## Steps
- [ ] 1. Extract the reservation (`_reserved_at` .. step 3) into
      `reserve_job_quota(db, *, job_id, user_id, want) -> int | None` in
      `src/workers/tasks.py`; `run_scrape_job` calls it. Behaviour unchanged.
- [ ] 2. `tests/test_quota_reservation.py`: `_reserve()` calls it; drop `_RESERVE_SQL`;
      rewrite the two `at=` tests; run the file green on the extraction.
- [ ] 3. Boundary lock-wait test; prove it FAILS on the step-1 extraction.
- [ ] 4. Fix: `account_charge_state()` returns `(block, now)` from one post-lock
      clock read; `account_charge_block` wraps it; the grant and `reserved_at` use
      that `now`. Test passes.
- [ ] 5. Targeted suites (`test_quota_reservation`, `test_audit4_paid_skip_trace_gate`,
      billing/settlement tests) + ruff on changed files.
- [ ] 6. Codex diff review until GATE: PASS. No push without owner OK.

## Review
(filled at the end)
