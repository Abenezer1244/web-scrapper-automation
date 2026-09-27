# 2b-i: account-level `run_eligibility` (UX audit Q6 / F-035)

Branch `feat/run-eligibility`, worktree `C:/Users/Windows/bl-wt/eligibility`.
Spec: FE `docs/ux-audit/phase-3.0-contracts.md` Q6. 2b-ii (per-config codes on the
scraper response, structured 402 bodies) is a separate, later step.

## Problem (confirmed in code)
- The account-level run gate is `quota_block_reason(user, now)` (`src/api/quota.py:130`). It
  returns prose only. The reset date exists only inside the string.
- `GET /billing/usage` (`src/api/routes/billing.py:653`) has no `can_run` / reason, so the FE
  re-derives the rule from 4-5 fields.
- `/billing/usage` reports the RAW `records_limit`, while the gate uses
  `effective_records_limit`. Across a boundary that carries a pending downgrade (window ended,
  lazy rollover not yet run), usage says `0 / 5000, Business, pending Pro` while the gate
  (and the next charge) says `0 / 1000, Pro`.
- The route returns a bare `dict`, so `schema/openapi.json` publishes
  `{additionalProperties: true}` and the FE keeps a hand-written `UsageResponse`.

## Found during investigation (fold in)
- **Wrong reset promise on a cancel-at-period-end account.** When a user is over the limit
  and `entitlement_ends_at <= window end`, the window does NOT reset at the window end
  (`should_roll` refuses: no entitlement left), the account becomes `ended` instead. Today
  the 402 text still says "Your quota resets YYYY-MM-DD". Under the new contract
  `resumes_at` must be `null` in that case, and the message must not promise a reset.

## Design
1. `src/api/quota.py`: add a frozen dataclass `RunEligibility(can_run, code, message,
   resumes_at)` and `run_eligibility(user, now=None) -> RunEligibility`. Same order as today:
   | code | when | resumes_at |
   |---|---|---|
   | `frozen` | `is_frozen` | null |
   | `ended` | `now >= entitlement_ends_at` | null |
   | `over_limit` | `is_over_record_limit` | `effective_window(user, now)[1]`, or null when `entitlement_ends_at` is set and `<=` that end |
   | (none) | otherwise | `can_run=True`, code/message/resumes_at null |
   One `now` is pinned once and passed to every helper (today each helper reads its own clock).
   The prose lives in module constants (Codex P3); `RunEligibility`/`run_eligibility` join
   `__all__`. Messages stay byte-identical to today's for the existing cases (callers and any tests
   that match the prose keep working); only the ended-at-boundary over_limit case drops the
   "resets" sentence.
2. `quota_block_reason(user, now=None)` becomes a thin wrapper:
   `e = run_eligibility(user, now); return None if e.can_run else e.message`. Callers
   (`jobs.py:316`, `batches.py:276`, `dispatch.py:129,385`, `batch_tasks.py:149`) unchanged.
3. `src/api/schemas.py`: `RunEligibilityResponse` (`code: Literal["frozen","ended",
   "over_limit"] | None`, `resumes_at: datetime | None`) with a `model_validator` enforcing:
   can_run -> code/message/resumes_at all null; not can_run -> code and message set; only
   `over_limit` may carry `resumes_at` (Codex P2). `UsageResponse` covers every field the
   route returns today, plus `run_eligibility`. Instants are typed `datetime` (OpenAPI
   `date-time`); `period_basis` and `payment_state` are `Literal`s. `GET /billing/usage` gets `response_model=UsageResponse`.
4. `/billing/usage` reports the EFFECTIVE view, from one pinned `now`:
   - `records_limit` = `effective_records_limit` (and `records_remaining` / `percent_used`
     computed from it).
   - `run_eligibility` added.
   - Instants serialize via Pydantic (`...Z`) instead of `isoformat()` (`...+00:00`). Same
     instant; the FE parses both with `new Date()`. Every instant (`period_start`,
     `next_reset_at`, `entitlement_ends_at`, `resumes_at`) goes through `as_utc` first, so a
     naive driver value is never published as local time (Codex P3).
   - **One clock (Codex P1).** The route pins `now = datetime.now(UTC)` once and passes it to
     `run_eligibility`, `effective_records_used`, `effective_records_limit`, `effective_window`,
     `should_roll`, `is_frozen`. No helper reads its own clock.
   - **`next_reset_at` is nullable (Codex P1).** `null` when `entitlement_ends_at` is set and
     `<=` the effective window end: that boundary ends paid access, it does not reset the quota.
     Verified that the live FE is safe: `BillingTab` renders it via `formatUtcDate`, which shows
     no label for null (`lib/utils.ts:102`).
   - Pending view (Codex P2, rounds 1+2): mirror the authoritative rollover SQL
     (`quota_window.py:322-326`) exactly, with `rolling = should_roll(user, now)`:
     `plan = pending_plan if rolling and pending_plan is not None else plan`;
     `records_limit = effective_records_limit(user, now)`; both pending fields reported null
     whenever `rolling`. The writer (`billing_entitlement.py:297`) always sets the pair
     together, and a downgrade to Starter is the string `"starter"`, never `None`. Otherwise the page shows "Business, 0/1000, downgrading to Pro" for
     a customer who is already on Pro in every way the gate and the next charge care about.
5. Regenerate `schema/openapi.json` in `.venv-schema` only. The `/billing/usage` 200 schema
   intentionally changes (bare object -> `UsageResponse`), and new components are added. Nothing
   else may change: no other path or component in the diff vs `origin/main` (Codex P2). FE regen + FE `UsageResponse` type follow in an FE PR once BE is on
   `main` (the FE drift gate reads BE main).

## Not in scope (2b-ii or later)
Per-config codes (`not_entitled`, `config_inactive`, `ai_limit`, `run_in_flight`), structured
402 bodies, the "remaining is below a typical run" warning, prod `ENTITLEMENT_ENFORCEMENT`.

## Tests (real DB, `bridgeleads_eligibility_test`; each proven RED on unfixed code)
`tests/test_run_eligibility.py`:
- [ ] ok user -> `can_run=True`, all else null.
- [ ] frozen (`FROZEN_STATUSES` status; and `past_due` past grace) -> `frozen`, resumes null.
- [ ] ended (`entitlement_ends_at` in the past) -> `ended`, resumes null; frozen wins over ended.
- [ ] over limit inside a live window -> `over_limit`, `resumes_at == window end`.
- [ ] over limit with `entitlement_ends_at` `==` AND `<` the window end -> `over_limit`,
      `resumes_at` null, message has no "resets"; same for the `POST /jobs` 402 body.
- [ ] unlimited (-1) never over.
- [ ] `RunEligibilityResponse` model rejects: can_run with a code; can_run with resumes_at;
      blocked with no code or no message; `frozen`/`ended` with resumes_at (Codex P2).
- [ ] naive-datetime user row (tz stripped) publishes the same UTC instants.
- [ ] `quota_block_reason` returns today's LITERAL prose (spelled out in the test, not read
      from the constants) for frozen, ended and live over_limit; the cancel-at-end over_limit
      text has no "resets" (Codex P2: an equality-with-itself test asserts nothing).
- [ ] exact boundaries with a fixed clock: `now == entitlement_ends_at` -> ended;
      `now == period_end` -> the new window (used 0, can_run).
- [ ] cancel-at-end with `entitlement_ends_at` `<`, `==`, `>` the window end: resumes_at /
      next_reset_at null, null, the window end.
- [ ] route: `GET /billing/usage` `run_eligibility` for ok, frozen, ended, over_limit users,
      with `resumes_at` serialized as an ISO instant or null.
- [ ] route: cancel-at-end user -> `next_reset_at` null.
- [ ] route: window ended + pending downgrade Business->Pro with stale used=3000 ->
      `records_limit == 1000`, `plan == "pro"`, pending fields null, `records_used == 0`;
      and Agency(-1)->Pro(1000): `records_remaining == 1000`, not null; and Pro->Starter and
      Agency->Starter pending pairs report `plan == "starter"` with the Starter limit.
- [ ] route: `POST /jobs` 402 body for frozen, ended and over_limit users is today's prose.

## Steps
- [x] Create `bridgeleads_eligibility_test` + env script (new Redis db index), `alembic upgrade head`.
- [x] Write tests; run on unfixed code, record RED.
- [x] Implement quota.py; schemas + route.
- [x] Tests GREEN; ruff; full suite in 8 parts.
- [x] openapi regen in `.venv-schema`. Parse both JSONs and diff structurally vs
      `origin/main`: the only changed path is `/billing/usage` GET 200, the only new
      components are `UsageResponse` and `RunEligibilityResponse`; nothing else added,
      modified or removed (Codex P3).
- [x] Security review (§14): read-only route, auth unchanged, own-row only, no new input.
- [x] Codex diff review until GATE: PASS.
- [ ] Quiesce check, merge. Prod verify (Codex P3): read-only query for which states exist
      in prod (frozen / ended / pending-downgrade / ok), then call `/billing/usage` as the
      owner and compare against `run_eligibility` computed from the same DB row; check
      Railway logs for `/billing/usage` 5xx / response-validation errors after deploy.
- [ ] FE follow-up PR (ships right after BE merges; the FE drift gate reads BE main):
      regen types; `UsageResponse.run_eligibility`; `next_reset_at: string | null`
      (Codex review P2: runtime already safe via `formatUtcDate(null)`); BillingTab shows
      `usage?.plan ?? user?.plan` so the plan agrees with the effective limit (Codex review P2).

## Codex consult
Round 1: NO-GO. P1 one clock in the route; P1 `next_reset_at` promises a reset on a
cancel-at-end account. P2 pending-view spec, OpenAPI diff wording, vacuous equality test,
route coverage, schema types/invariants. P3 `__all__` + constants, prod verify. All adopted
above. No disagreements.
Round 2: NO-GO on P2s only (round-1 P1s confirmed resolved). P2 pending->Starter spec (now
mirrors the rollover SQL, which I verified), P2 `<` case for the no-reset text + 402, P2
negative model tests; P3 structural OpenAPI diff, P3 `as_utc` on every instant. All adopted.
Round 3: **PLAN: GO**, no findings.

## Review
Codex diff review round 1: GATE: FAIL, no P1. Adopted: P2 `next_quota_reset` returns null
while frozen (a frozen window does not advance, so its stored end can be in the past) — fixed,
mutation-proven; P3 exact-instant assertions in the naive and HTTP tests; P3 gate coverage —
added POST /batches 402 tests; scheduler / batch dispatch / batch fire are already covered by
`test_dispatch_due_jobs:101`, `test_batch_dispatch:141`, `test_batch_2b_scheduled:150`.
Deferred to the FE follow-up (not BE defects; BE must land first for the drift gate): the FE
`next_reset_at` type and BillingTab reading `user.plan`.
Round 2: GATE: FAIL, but every finding was MY artifact: I sent a two-dot diff against an
`origin/main` that had moved (#366/#368 merged after the branch point), so main's new code
read as reverts. Rebased onto `165c3257`; full suite re-run on the rebased branch: 4914 passed,
0 failed. Codex ruled the FE sequencing acceptable. Lesson: always review `origin/main...HEAD`.
Round 3 (correct three-dot diff): **GATE: PASS**, no P1/P2; rebase interaction safe. One P3
left open on purpose: over-limit prose templates are still inline f-strings (cosmetic).
