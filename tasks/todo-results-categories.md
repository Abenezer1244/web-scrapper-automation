# Results page: "already delivered" / "combined" semantics (D1-D5)

Branch `investigate/results-categories` (worktree `bridgeleads-worktrees/results-categories`).
Owner instruction 2026-09-13: "solve all D1-D5 with codex".

## Findings (verified in code + read-only prod)

- **Already delivered** = `is_duplicate AND duplicate_reason='prior_run'`: this user already holds a
  `delivered_records` claim on the same frozen `sha256(parcel|address)` (else NAME|DATE), any record type,
  any county, no expiry. "Delivered" really means "claimed by an earlier run that kept the claim".
- **Combined** = `duplicate_reason='same_run'`: rows in one run with the same strong hash; one survivor.
- Tenant isolation holds (0 violations on 4 schema-wide checks). Duplicates never billed. CSV = new rows only.

## Codex design gate (3 rounds)

- Round 1 FAIL: v2 claim key built from scraped address / event tokens is unsafe (mass re-billing when an
  event id appears on a later scrape); claim DELETE for D1 unsafe (mailing backfill resurfaces the old row).
- Round 2 FAIL: D3 without D4 is inconsistent (two events billed twice in one run, once across runs) ->
  D2/D3/D4 move to a staged claim-ledger program. D1 needs cutoff evidence, weak-hash exclusion, stale claims.
- Round 3: B2 residual accepted as P3; three P1s resolved in implementation (see below).

## Phase A (implemented, local commits, NOT pushed)

- [x] A1 D5 `6b91a88`: skip-trace enqueue moved after re-election + plan cap; export rows reloaded with
      populate_existing for every plan; dispatcher withdraws rows that became duplicate/over-quota
      (pending 'cancelled', result 'not_attempted'). Tests: 3 enqueue + 3 dispatcher; mutation-proven.
- [x] A2 D1 `cc113c4`: `transfer_undelivered_claims` moves a claim (never deletes) when the claim is strong
      (from claim-time inputs), the anchor exists, is not actionable, and its run is failed/cancelled or done
      and billed after 2026-09-03 12:05:28 UTC. Old anchor -> 'superseded'. 16 tests incl. race, cap release,
      backfill, tenant; each guard mutation-proven.
- [x] A3 `9ea903b`: Results API excludes 'superseded' from counts and duplicate_sources; invariant checker
      knows the value. 1 API test, mutation-proven.
- [x] Merged origin/main (4 commits incl. #275 wider mailing recovery); full suite 2,985 passed; only
      failures = 7 test_plan_entitlement_audit (identical on clean main, local Stripe price env) + 1 auth
      lockout flake (passes alone and as its file).
- [x] Codex diff review rounds 1-5: 7 P1s + 2 P2s found and fixed (cancelled-run claims, recheck race, partial counts, done-job guard, refetch-failure billing, traces for failed jobs, lock inversion, claim sweep, billed failed anchors). See docs/HANDOFF-results-categories-2026-09-13.md.
- [x] Codex review rounds 6-9 + full suite after each fix (see Review).
- [ ] Codex review round 10 on `effe66e` (two attempts killed by memory pressure).
- [ ] Owner approval -> push + PR.

## Phase B (NOT started; needs owner approval of billing semantics): D2 + D3 + D4 claim ledger

Codex-agreed program, each step <= 5 files, each gated:
1. Identity spec + fixtures per scraper (instrument/recording/document id, case/record number, ts_number;
   tax = property-level only; King JSON vs DOM key aliases; EagleWeb/Skagit weak ids excluded).
2. Additive schema: `delivered_events` ledger (user, record_type, jurisdiction, canonical parcel, event
   token, alias/unknown), privileged migration, grants in provision_rls_roles.sql, out-of-band indexes.
3. Shadow mode: compute and record event identities on every run, no billing change; report divergence.
4. D2 cutover: claims scoped by record type for NEW properties; legacy claims keep cross-type suppression.
5. D3 + D4 cutover together: a new event = new lead only when every prior event for the property has a
   known, different token; unknown tokens fall back to property suppression.
Billing consequences to approve first: same property in two lists = two charges; a new filing/case/TS
number on a delivered property = a new charge; two cases on one parcel in one run = two charges.

## Review
Rounds 6-9 (2026-09-13): 2 P1 + 2 P1 + 1 P2 + 1 P2 + 1 P2 fixed, each with a test that failed first
(`0043390`, `dc41cd7`, `304d7e3`, `59666b2`, `effe66e`); one round-7 P1 downgraded to P3 with Codex's
agreement. Full suite on `effe66e`: 3,020 passed, 7 known entitlement failures. §14 security review: clean
(two passes). NOT done: Codex round 10 (memory), owner approval, push/PR. Details: BUILD_JOURNAL 2026-09-13
and docs/HANDOFF-results-categories-2026-09-13.md §8.
