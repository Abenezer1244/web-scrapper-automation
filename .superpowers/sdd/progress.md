# Tier Enforcement — Progress Ledger

Branch: worktree-tier-enforcement (base origin/main 91463c7)
Plan: docs/superpowers/plans/2026-06-22-tier-enforcement.md

Safe test env (subagents MUST export before pytest; NEVER use prod .env):
  ENVIRONMENT=test
  SECRET_KEY=test-secret-key-thirty-two-chars-minimum-0123456789
  DATABASE_URL=postgresql+asyncpg://localhost/nonexistent_test
  DATABASE_URL_SYNC=postgresql://localhost/nonexistent_test
  REDIS_URL=redis://localhost:6379/0

DB-write tests (reconciliation/race): gate behind RUN_DB_TESTS=1 + dedicated TEST DSN; never run vs prod.

## Status
- [x] Phase 1 / Task 1: complete (108f373, review clean; Minor: redundant matrix assert, no Agency test, _plan StopIteration — defer to final review)
- [x] Phase 2 / Task 2: complete (37fd3a0, review clean)
- [x] Phase 3 / Tasks 3-8: execution-time guards (audit mode) — d19a3d2,6025fd9,cf73876,0ee676c,52bca7c
- [x] Phase 4 / Task 9: TOCTOU advisory lock — 3480526 + centralized/gated fba2460 (Codex P3)
- [x] Phase 5 / Task 10: PULLED FORWARD — paused_reason migration (2b27e80, history 070 on 069 single head)
- [x] Phase 5 / Task 11: reconciliation wrappers + hooks — 393ae47 + gated 9262be1 (Codex P1)
- [x] Phase 6 / Task 12: audit script (d4dbde6) — FLIP is ops (post-deploy)
- [x] Phase 7 / Task 13: live verification — DONE (local headed-Chromium e2e, enforcement ON)

## Phase 1 gate
- Implementer DONE (108f373); task-reviewer Spec OK/Quality approved (3 Minor deferred).
- Codex review: 1 P2 (dialer-delivery copy) RESOLVED — dialer IS enforced (scrapers.py:139, dialer.py:118-126). No P1/High.
- Phase 1 CLEAN. Awaiting user approval for Phase 2.

## Phase 2 gate
- Implementer DONE (37fd3a0, 8 passed); reviewer Spec OK/Quality approved.
- Minor (DEFER to final review): no test for plan=None fail-closed; no test for empty rows=[]. Code DOES implement both (visible in diff), just uncovered.
- ⚠️ resolved: pro=3 counties, agency=-1 (unlimited), pro types exclude divorce — confirmed.
- Codex P2 (older paused county evicting newer active) FIXED in e9cd3a6: active claims slots first, paused fills remainder + regression test. Codex re-confirm: clean. _candidate_rows deleted (dead). Phase 2 CLEAN.

## Phase 3 gate
- 6 guards wired audit-mode + migration 070. Reviewer Spec OK. Codex P1 (worker guard before CAS could fail live job) FIXED 52bca7c (moved after claim), Codex re-confirm clean.
- DEFERRED P2: partial-batch visibility — blocked children invisible (run can read done not partial). Only post-flip; fix with finalize_batch_run + reconciliation in Phase 5/6.
- DEFERRED P3: guard query could throw in audit mode if code live before migration 070 — deploy migration-FIRST; consider audit-mode fail-open wrapper at flip (Phase 6).
- DEFERRED Minor (final review): N+1 active-configs query per item in scheduler/batch loops (hoist per-user); entitlements plan=None/empty-rows tests.

## Phase 4 gate
- Per-user advisory xact lock closes county-count race. Codex verified it actually serializes (one txn). P3 (audit-mode needless serialization) FIXED: lock centralized in enforce_entitlements, gated by ENTITLEMENT_ENFORCEMENT. 10 passed/1 skipped (race test gated on RUN_DB_TESTS).

## Phase 5 gate
- Reconciliation (pause over-limit / revive on upgrade) wired to Stripe updated/deleted + trial-expiry. Reviewer+Codex P1: not gated by flag (would mutate in audit mode) FIXED 9262be1 (dry-run log + return 0,0 when flag off). Codex re-confirm clean. 10 passed/1 skipped.

## FINAL whole-branch review
- Audit script d4dbde6. Final reviewer (opus): READY-WITH-FOLLOWUPS, no blockers. Codex final: 2 findings — P1 API-key gate inconsistency (made always-on) + P2 disallowed-type configs consuming county slots (type-filter). Both FIXED ba41278, Codex re-confirm clean. 19 passed/2 skipped, ruff clean.
- Followups tracked PRE-FLIP: P2 partial-batch finalize visibility; P3 deploy migration-first; N+1 loop query.

## Phase 7 — frontend gate UI + local headed e2e (2026-06-22/23)
- FE worktree `bridgeleads-web/.claude/worktrees/tier-gate-ui` (branch tier-gate-ui off master). 4 core changes committed (`1b8c0e5`): NEW lib/entitlements.ts (mirrors constants.py; canUseRecordType fails CLOSED for unknown types); lib/errors.ts 402-aware (toastUpgrade + Upgrade->billing action; 401/403 forced generic); CountyStep premium record-type chip locking (single+batch); new/page.tsx fresh ["me"] plan (staleTime:0 + refetchOnMount, fail-closed-to-starter until mount refetch lands via dataUpdatedAt gate) + drop-disallowed-types + exit-batch/shed-premium-values on downgrade. tsc + eslint clean.
- Codex reviewed twice: 6 findings (5×P2 + 1×P3) — ALL fixed + re-confirmed (the P2-2 cache-staleness fix added the dataUpdatedAt freshness gate).
- LOCAL stack: throwaway PG (127.0.0.1:55432/bridgeleads_test, create_all, 24 tables, 12 WA connectors seeded), **standalone Windows Redis** (tporadowski v5 in /tmp/redis-win on :6379), API on :8000 with ENTITLEMENT_ENFORCEMENT=true, FE dev on :3000. Env in /tmp/e2e_env.sh.
- 🛑 GOTCHA (HANDOFF Part C was WRONG): "no Redis needed" is FALSE — auth token-revocation check (auth.py:339-355) FAILS CLOSED (503) when Redis is down, so ALL authenticated requests 503 without Redis. rate_limit fails open, but auth does NOT. Prod has Redis so this is local-test-only, but any future local run needs Redis up.
- Headed Chromium 5-persona walkthrough (trial/starter/pro/business/agency): record-type chip locks + batch gating match the matrix EXACTLY for all 5 — ALL PASS. Screenshots in C:/Users/.../Temp/e2e_shots/.
- Backend 402s verified (curl + UI): Pro 4th distinct county -> 402 ("...span 4 distinct counties but your 'pro' plan allows 3..."); Pro divorce -> 402 (record-type). UI: real wizard Save as Pro (3 existing configs) -> backend 402 -> toastUpgrade with exact detail + working "Upgrade" action; Pro Delivery step shows Webhook 🔒 Business+ locked. Test users pw=BridgeE2E2026! (trial/starter/pro/business/agency @example.com).

## P2 fix (post-PR)
- 4c1c572: partial-batch visibility. fan-out records blocked_children (config_id+county+type+reason) in both branches; finalize fresh-reads + merges prior_blocked into failed + total -> correct done/partial/failed; get_batch maps blocked configs to failed not pending. Design Codex-approved pre-build (no P1s). 18 tests pass, ruff clean. 🛑 Codex POST-build confirm PENDING: Codex usage limit hit (resets Jun 24). Implementation manually verified vs approved design.
