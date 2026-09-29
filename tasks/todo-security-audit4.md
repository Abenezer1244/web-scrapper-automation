# Security audit #4, Phase 4a: paid skip trace only for accounts that may run it (S3-03, S4-01)

Report: `SECURITY-AUDIT.md` (Audit #4 section). Owner decisions 2026-09-27: delta audit + fixes; trials get a
**small fixed allowance** of contact lookups; start 4a. One PR per phase; merging deploys, so stop before merge.

## Rule (one statement: `paid_lookup_access`, src/workers/skip_trace_claim.py)

| Account (first match wins) | Lookups |
|---|---|
| Starter plan | none |
| frozen (`is_frozen`) | none (queued rows held, sent once payment clears) |
| paid term ended (`entitlement_ends_at <= now`) | none (queued rows withdrawn) |
| admin | full |
| paid term ends later (cancelled at period end) | full |
| `active`, or `past_due` with a grace deadline | full |
| no status AND no trial date (operator-granted) | full |
| anything else (app trial, `trialing`, `canceled`, unknown) | `SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE` credits, lifetime (default 25; 0 = none) |

## Done

- [x] `src/config/settings.py`: `SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE` (validated non-negative)
- [ ] `.env.example`: add `SKIP_TRACE_TRIAL_CREDIT_ALLOWANCE=25` with a comment. NOT DONE: a permission rule in
      this environment blocks reading `.env.example`; owner or a follow-up adds the line.
- [x] `src/workers/skip_trace_claim.py`: the rule, `read_access_rows`, `lifetime_credits_queued`; the claim
      holds what the account may not buy, under a `FOR NO KEY UPDATE` lock on the user row
- [x] `src/workers/skip_trace_dispatcher.py`: unlocked prefilter (Starter/ended rows withdrawn, frozen held),
      locked `FOR SHARE SKIP LOCKED` re-check of the selected accounts before the claim
- [x] `src/workers/tasks.py`: `account_charge_block`; refuse frozen/ended after the job claim; re-decide at the
      reservation under the users lock with a clock read after the lock (grant 0); blocked accounts enter the
      cap block even when unlimited
- [x] `tests/test_audit4_paid_skip_trace_gate.py` (43 tests, real DB, 2 concurrency tests); every guard
      mutation-checked. `test_skip_trace_dispatcher_claim.py` / `test_skip_trace_spend_ledger.py`: fixture
      `starter_user` -> `business_user` (a Starter account's queued rows are now withdrawn by design)
- [x] Codex: plan consult (2 P1s folded in) + review rounds 1-4
- [x] Full suite on a fresh `_test` DB (6 foreground batches) + re-run of every dispatcher test after the last
      change; ruff clean; no API/schema change, so no OpenAPI regen

## Moved to 4b

- Customer-facing live-log line when leads are held by the trial allowance (enrich.py, `report=`)
- S4-03: four export routes into the `export` rate-limit zone

## Review (4a)

See `SECURITY-AUDIT.md`, "Phase 4a". Residuals: lifetime count uses queue rows (operator scripts only can
delete them); an unlimited account freezing after the gate read is delivered that run; unlimited path has no
automated test; S4-05 and S4-06 are pre-existing quota bugs found on the way, logged OPEN.
