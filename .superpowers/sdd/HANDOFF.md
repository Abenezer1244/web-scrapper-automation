# Tier Enforcement — Session Handoff (2026-06-22/23)

## TL;DR
Backend tier-enforcement is **DONE + on PR #109** (audit-mode, Codex-verified). Current task: **build the frontend gate UI + run a LOCAL headed-Chromium end-to-end test as 4 tiers** (free-trial / Pro / Business / Agency). Decision: test LOCALLY with enforcement ON against a throwaway DB — NOT a prod merge/flip (that's a later deliberate step needing the audit script first).

---

## PART A — Backend (COMPLETE, do not rebuild)

- **PR #109** `feat/tier-enforcement` → `main` (repo Abenezer1244/web-scrapper-automation). 17 commits. Ships AUDIT-MODE (`ENTITLEMENT_ENFORCEMENT` default False = zero behavior change).
- Backend worktree: `web-scrapper-automation/.claude/worktrees/tier-enforcement`, branch `worktree-tier-enforcement` (pushed as `feat/tier-enforcement`). Base origin/main `91463c7`.
- What it does: per-tier access as an EXECUTION-TIME invariant. Matrix (= `src/config/constants.py`, canonical):
  - Starter(free): 1 county, `probate` only
  - Pro($199): 3 counties, `probate`/`pre_foreclosure`/`tax_delinquent`
  - Business($499): 10 counties, all 6 types + overlap + webhook/dialer/API
  - Agency($1499): unlimited, all + white-label
  - counties = count cap (user picks WHICH); record types = capability menu.
- Pieces: pure helpers in `src/api/entitlements.py` (`allowed_county_set`, `config_run_violation`, `plan_reconciliation` + `ConfigRow`); guards at 6 sites (POST /jobs, worker job-start AFTER the CAS, scheduler dispatch, batch fan-out, generic webhook send, API-key auth); downgrade reconciliation (pause/revive, `paused_reason` col = migration 070); per-user advisory lock (centralized in enforce_entitlements, flag-gated); honest `/billing/plans` copy; read-only `scripts/audit_entitlement_violations.py`.
- Codex caught + we fixed a real bug in 5 spots (eviction P2, worker-CAS P1, audit-lock P3, reconciliation-gate P1, API-key/county-slot P1/P2) + partial-batch P2 — ALL re-confirmed clean. 19 unit tests pass, ruff clean.
- Ledger: `web-scrapper-automation/.claude/worktrees/tier-enforcement/.superpowers/sdd/progress.md`. Plan: `docs/superpowers/plans/2026-06-22-tier-enforcement.md`. Spec: `docs/superpowers/specs/2026-06-22-tier-enforcement-design.md`. Memory: `project_tier_enforcement_2026_06_22.md`.
- 🛑 PROD ROLLOUT (later, user-gated): deploy migration 070 BEFORE code → run audit script vs prod → grandfather (~0 paying) → set `ENTITLEMENT_ENFORCEMENT=true` on API+worker → live-verify. DO NOT flip prod without the audit step.

## PART B — Frontend gate UI (IN PROGRESS)

- FE repo: `Desktop/bridgeleads-web` (SEPARATE repo, default branch = `master`). 🛑 A concurrent session has UNCOMMITTED WIP on `feat/schedule-day-picker` (modifies page.tsx, lib/api.ts, lib/types.ts) — DO NOT touch the main checkout.
- **FE WORKTREE READY:** `bridgeleads-web/.claude/worktrees/tier-gate-ui`, branch `tier-gate-ui` off origin/master `1f9172b`. `node_modules` is a JUNCTION to the main checkout's node_modules (don't reinstall). `.env.local` copied (NEXT_PUBLIC_API_URL=http://localhost:8000). node v22.16.
- Codex-designed scope (4 core + secondary):
  1. NEW `lib/entitlements.ts` — single source: PLAN_RANK, COUNTY_CAP, RECORD_TYPE_MIN_PLAN {probate:starter, pre_foreclosure:pro, tax_delinquent:pro, code_violation:business, divorce:business, death_certificate:business} (unknown e.g. `eviction` fails CLOSED to business), helpers canUseRecordType/countyCap/canBatch/canUseWebhook/canUseOverlap/canUseApiKeyPlan, PLAN_LABEL.
  2. `lib/errors.ts` — 402-aware: getFriendlyError shows backend `detail` verbatim for status 402 (keep leak-detection); new `toastUpgrade(error)` = toast.error(detail,{action:{label:"Upgrade",onClick:()=>window.location.assign("/settings?tab=billing")}}); toastError delegates to toastUpgrade when status===402. Do NOT treat 403 globally as upgrade.
  3. `app/(dashboard)/scrapers/new/_steps/CountyStep.tsx` — lock premium record types per plan in BOTH single + batch modes using existing `Chip` `locked`+`tooltip`; drop already-selected types the plan no longer allows; connector availability stays the base list.
  4. `app/(dashboard)/scrapers/new/page.tsx` — use FRESH plan from the `["me"]` react-query (getMe), NOT stale `session.user.plan` (JWT is 7-day stale); pass down to steps.
  - SECONDARY (Codex P1/P2, do if time): fix marketing pricing copy (`app/(marketing)/_monopo/data.ts:~172` promises all types/no county caps); fix false trial copy `components/shell/ShellMain.tsx:~25` ("PRO TRIAL / all features unlocked"); gate `/segments` overlap to Business+ (`app/(dashboard)/segments/page.tsx`, `lib/nav.ts`, and the export 402 detail at `lib/api.ts:~465`); Quick Start 402 detail (`app/(dashboard)/dashboard/page.tsx:~76`).
- Existing FE gates already correct (reuse pattern): batch (Pro+) CountyStep, skip-trace (Pro+) FieldsStep, webhook/dialer (Business+) DeliveryStep, quota-upgrade-banner, canUseApiKey in lib/utils.ts.
- ⚠️ BLOCKER hit by the async build subagent: its sandbox denied Bash/PowerShell/Write — it could only Edit EXISTING files. To let an agent create `lib/entitlements.ts`, the file must be `touch`ed first; OR just build all 4 inline with the Write/Edit tools directly (recommended). Nothing was committed by it yet — verify with `git -C <fe-worktree> status`.
- VERIFY FE: `cd <fe-worktree> && npx tsc --noEmit` and `npx eslint <changed files>` must be clean. Then commit (NOT node_modules/.env.local).

## PART C — Local stack for the headed test

- **Local Postgres: conda env `pgtest`** binaries at `C:/Users/Windows/anaconda3/envs/pgtest/Library/bin` (initdb/pg_ctl/createdb/psql/postgres). A throwaway instance was started: data dir `C:/Users/Windows/AppData/Local/Temp/pg_tier_e2e`, **port 55432**, trust auth, db `bridgeleads_test`, user `postgres`. (Re-verify it's up: `python -c "import socket;s=socket.socket();s.connect(('127.0.0.1',55432))"`; restart with `"$PGBIN/pg_ctl.exe" -D <data> -l <log> -o "-p 55432 -c listen_addresses=127.0.0.1" start`; create db if missing.)
- **No Redis** — and none needed: rate_limit FAILS OPEN on Redis errors (`src/api/middleware/rate_limit.py:154`), and job-enqueue failure is non-fatal (`jobs.py` catches it). Point REDIS_URL at redis://localhost:6379 (nothing there) → fail open.
- **Backend run plan:** build schema with `Base.metadata.create_all` against the test DB (SKIP Alembic/RLS — tier gates use explicit user_id filters, so RLS-less is fine; `paused_reason` is in the model so create_all includes it). Then run the API (`_local_dev_api.py` exists, OR `uvicorn main:app --port 8000`) with env:
  - ENVIRONMENT=test (or dev), SECRET_KEY=<32+ chars>,
  - DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:55432/bridgeleads_test
  - DATABASE_URL_SYNC=postgresql://postgres@127.0.0.1:55432/bridgeleads_test
  - REDIS_URL=redis://localhost:6379/0
  - **ENTITLEMENT_ENFORCEMENT=true**  ← the whole point
  - ALLOWED_ORIGINS to include the FE origin (http://localhost:3000) — CORS. (CSP in FE next.config.ts only allows backend port 8000, so run API on 8000.)
- 🛑 NEVER point the backend at the prod `.env` (it's PROD; tests/conftest teardown WIPES tables). Use ONLY the local 55432 test DB.

## PART D — Headed Chromium e2e (the goal)
Drive a HEADED Chromium (Playwright / the `browse`/`gstack` skill) against FE `http://localhost:3000` (FE pointed at API :8000, enforcement ON). For each of 4 personas, sign up / set plan, then verify per the matrix, capturing screenshots:
1. **Free trial**: register via UI → Pro-trial; verify trial badge + Pro-level access (3 counties, core types); then expire trial (set trial_ends_at past + run downgrade, or DB-set plan=starter + reconcile) → Starter limits + paused configs.
2. **Pro**: 3 counties OK / 4th → 402 upgrade prompt; core types OK / divorce locked; batch OK; skip-trace OK; webhook locked; no API key.
3. **Business**: 10 counties, all types incl divorce, webhook, API key, overlap/segments.
4. **Agency**: unlimited.
(Set non-trial plans by DB UPDATE on the local test DB — no real Stripe. e.g. psql `update users set plan='business', records_limit=5000 where email=...`.)

## Workflow rules in force (user-mandated)
- Consult + build WITH Codex on every move; verify each other's work with Codex (`codex exec ... -s read-only`). Codex CLI = gpt-5.5, works now.
- Isolate all work in worktrees (shared OneDrive dir has concurrent sessions flipping HEAD / editing FE WIP). Never delete/force branches.
- No guessing; fix from root cause; phased; tests real (no mocks).
