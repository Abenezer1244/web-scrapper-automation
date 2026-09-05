# Quota counter silently reset — root cause + fix

Branch: `fix/usage-quota-audit`  Worktree: `C:/Users/Windows/bridgeleads-worktrees/quota-audit`

## Confirmed production evidence

`users.records_used` is the authoritative stored counter; `/billing/usage` renders it
verbatim. `X / 1,000` = **records consumed this calendar month**.

Two DISTINCT defects, both proven against prod data:

### Defect 1 — new users are wiped (the `IS NULL` arm)
`users.records_period_start` is nullable with **no server_default**; migration 020
backfilled only pre-existing rows; the sole creation site `_create_real_user` never
sets it. So every new user has `records_period_start = NULL`, and the daily
00:05 UTC rollover matches its `records_period_start IS NULL` arm and zeroes
`records_used` — inside the same calendar month, with no billing event.

Account `01dc9396` (created 2026-09-02): billed 999 records by 2026-09-02 09:55,
wiped to 0 by the 2026-09-03 00:05 run, then +0 (Sep 4) +2 (Sep 5) = **stored 2**,
against a ledger of **1001**. Exact match.

### Defect 2 — a LATE rollover destroys new-period usage
The reset zeroes unconditionally. When Beat misses the 1st (the exact scenario the
daily-catch-up design was added for), the late run wipes usage already billed
*inside the new period*.

Account `b6d2095d` (created 2026-03-24, so NOT the NULL bug): billed 67 on
2026-09-02, wiped by the 2026-09-03 00:05 late rollover, then +73 = **stored 73**
against a ledger of **140**.

Blast radius: 2 of 4 users, **1,066 records under-counted**. `01dc9396` also
silently exceeded its 1,000 cap (ledger 1001).

## Authoritative source of truth
`jobs.billed_count` where `billing_applied_at IS NOT NULL` — a durable per-job
billing anchor written under a CAS. `records_used` is a cached rollup of it.
Usage is therefore deterministically reconstructible.

## Design (makes zeroing provably safe)
Roll the period forward **at billing time**, atomically, in the same statement that
increments. Then a stale `records_period_start` *proves* no job billed this period,
so the daily task's zeroing is correct by construction. The daily task becomes a
safety net for non-billing users rather than the sole mechanism.

## Phase 1 — stop the loss (5 files)
- [ ] `src/db/models.py` — `server_default` + `nullable=False` on both period columns
- [ ] `src/api/routes/auth_helpers/registration.py` — stamp both period starts at creation
- [ ] `src/workers/scheduler_helpers/billing.py` — non-destructive reset:
      NULL is ADOPTED (stamped, not zeroed); stale is rolled over; and skip-trace
      is gated on `skip_trace_period_start`, not `records_period_start` (latent bug)
- [ ] `src/workers/tasks.py` — period-aware atomic billing increment
- [ ] `alembic/versions/086_*` — backfill NULLs without zeroing, then NOT NULL + default

## Phase 2 — tests + historical repair
- [ ] Rewrite `tests/test_workers.py:579+` (they currently ASSERT the bad behavior)
- [ ] New quota tests: 0/1000, increment, 999->1000, cap, legit rollover, no
      premature reset, NULL adoption, late rollover preserves new-period usage,
      delete-does-not-decrement, failed/partial/retry, batch, concurrency
- [ ] `scripts/repair_records_used_from_ledger.py` — recompute from the anchor,
      `--dry-run` default, period-scoped, fail-loud

## Phase 3 — over-allocation reservation (separate reviewed change)
Cap and billing are in SEPARATE transactions with the export between them, so a
`FOR UPDATE` cannot span them. Correct fix = atomic reservation at cap time +
idempotent billing + release-on-failure in 3 error paths. Deferred deliberately.

## Decisions
- Calendar-month reset KEPT (matches "1,000 records/month" copy). Stripe-anniversary
  divergence documented as a known gap, not changed. (user decision)
- Concurrency over-allocation: fix approved, scheduled as Phase 3. (user decision)
