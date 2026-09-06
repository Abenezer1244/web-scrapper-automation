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

---

## Review

### What `2 / 1,000` meant
Records **CONSUMED** this calendar month. Confirmed live: `/billing/usage`
returned `records_used: 2, records_limit: 1000, records_remaining: 998,
percent_used: 0.2`, and the UI rendered `2 / 1,000` on both dashboard and
settings. The backend itself reports 2 — a backend accounting defect, not a
frontend rendering one. Refresh and logout/login left it unchanged.

### Verified correct usage
**1,001 records** for account `01dc9396` in the 2026-09 period, from
`SUM(jobs.billed_count)` over 16 jobs with `billing_applied_at` in September.
That is ABOVE the 1,000 cap — the account silently exceeded its plan.
Second account `b6d2095d`: **140**, stored 73.

### Was it a legitimate reset?
**No.** `records_period_start` moved from NULL to 2026-09-01 — the same month
the usage occurred in. No billing period rolled and no Stripe event fired.

### Root cause
Two defects in `_reset_monthly_usage_impl`, both proven against prod:
1. `records_period_start` was NULL on every user registered after migration
   020 (nullable, no server_default, never set at creation), and the rollover's
   `IS NULL` arm zeroed them mid-month.
2. The rollover zeroed unconditionally, so a late catch-up run after Beat
   missed the 1st wiped usage already billed inside the new period.

### Architecture
Authoritative source = `jobs.billed_count` + `jobs.billing_applied_at`, a
durable per-job anchor written under a CAS. `users.records_used` is a cached
rollup of it and is therefore deterministically reconstructible. Usage is NOT
derived from visible rows, batches, or any UI aggregation.

### Verification
- Full suite **2327 passed, 2 skipped** (CI's exact target).
- `ruff` clean across `src/`, `tests/`, `scripts/`, `alembic/`.
- Migration 086 applies cleanly on a fresh DB.
- Live headless-Chromium check against production (above).

### Notes
- The new tests caught a **timezone bug in my own fix**: naive
  `date_trunc(...)` compared to a `timestamptz` column is re-interpreted in the
  session zone, which under a negative UTC offset reads a current-period user
  as stale and zeroes them. Every boundary now carries the `AT TIME ZONE 'UTC'`
  re-cast.
- Codex caught a **[P1] I missed**: Postgres `NOW()` is transaction-start time,
  so a transaction straddling the month boundary broke the central safety
  claim. Fixed with one bound `clock_timestamp()` for both statements.
- Independently found (before Codex reported the same): the repair script must
  skip STALE periods, and must not DECREASE a counter by default — a deleted
  job leaves the ledger, and deleting data must never refund quota.

### Shipped
- [x] **PR #223 merged `46c8ed1` + DEPLOYED** (Build & Push + Run Migrations
      green, so migration 086 is live).
- [x] **Repair APPLIED and CONVERGED**: 2 users repaired, 1,066 records
      restored, post-repair drift 0. `01dc9396` 2 -> 1001, `b6d2095d` 73 -> 140.
- [x] **Live-verified in the UI**: `1,001 / 1,000`, `records_remaining: 0`,
      `percent_used: 100.1`, stable across refresh and logout/login. Displayed
      usage, backend usage and the billing ledger now all agree.
- [x] **Phase 3 — PR #224 merged `f4934ab` + DEPLOYED** (migration 087 live).
      Cap now RESERVES atomically; billing settles the delta; release on every
      failure path plus a state-based beat sweep for paths that bypass them.
      Codex found 4 P1s in it (lock-order inversion, non-concurrency-safe
      release, cross-period settlement, terminal paths with no release) — all
      fixed. Full suite 2345 passed / 2 skipped.

### Note on the repaired account
`01dc9396` now reads 1,001 / 1,000 and is therefore **over its cap**, so it
cannot start new scrapes until the Oct 1 rollover. That is the correct number —
it genuinely consumed 1,001 — not a residual bug.
