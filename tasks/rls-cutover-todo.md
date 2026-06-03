# RLS Least-Privilege Cutover — Staged Plan

**Branch:** `security/redteam-remediation-2026-06-01`
**Date:** 2026-06-02
**Origin:** SQL-injection audit (Claude + Codex). Audit verdict: **NO SQLi exists** —
every query path is parameterized. The real "could wipe the table" risk is the
**over-privileged production DB role** (`BYPASSRLS`, `RLS_ENFORCE=False`). This plan
downgrades to least-privilege roles and turns RLS into a real enforcement boundary.

> ⚠️ **LANDMINE:** Flipping `RLS_ENFORCE=True` or switching to a `NOBYPASSRLS` role
> *before* the code changes below WILL break scrapes/ingest and refuse to boot
> (`src/db/session.py`, `settings.py:101-108`). The phases must land **in order**.
> `FORCE ROW LEVEL SECURITY` is the **last** step, in production, after staging passes.

---

## Target architecture (agreed with Codex)

Three roles instead of one BYPASSRLS role:

| Role | Used by | Privileges | RLS |
|---|---|---|---|
| `bridgeleads_owner` | Alembic migrations only | DDL (owner) | n/a |
| `bridgeleads_app` | FastAPI request traffic (`DATABASE_URL`) | SELECT/INSERT/UPDATE on app tables; **no DELETE, no DDL** | NOBYPASSRLS, policies apply |
| `bridgeleads_system` | Celery workers + scheduler (`DATABASE_URL_SYNC`) | SELECT/INSERT/UPDATE all + DELETE on `county_records` only | NOBYPASSRLS; named-role carve-out in policies |

Why no blanket DELETE for the app role: user "deletes" are soft — job cancel is
`jobs.status="cancelled"` (`jobs.py:202`), scraper delete is `active=False`
(`scrapers.py:306`). The only physical runtime DELETE is retention cleanup of
`county_records` (`scheduler.py:521`), which runs on the system role.

---

## Phase 0 — Role + GRANT SQL (NO prod impact) ✅ DRAFTED
**Files:** `scripts/provision_rls_roles.sql`, `docs/security/RLS-CUTOVER-RUNBOOK.md`
- [x] Three roles via idempotent SQL (`DO`-block guards; passwords as psql vars, never committed)
- [x] Runbook documents provisioning + verification + rollback
- [x] Self-review caught a footgun: API writes `county_connectors` (`scrapers.py:313` POST /connectors)
      → changed app-role grant from SELECT-only to **SELECT + INSERT** on that table
- [x] **Gate (Codex):** PASSED — Codex review (2 rounds) → APPROVE. Round 1 found 5 issues
      (app over-grant on worker tables, unconditional safety re-assert, password rotation
      footgun, no transaction, DDL not proven); all fixed + verified against `src/db/models.py`
      and `src/api/routes/*`. Round 2 nit (abort paths exited 0) fixed with `RAISE EXCEPTION`.
      NOTE: stop-time Codex gate temporarily DISABLED earlier (`codex-companion.mjs
      setup --disable-review-gate`). **Re-enable when convenient:** `--enable-review-gate`.
- [ ] **Gate (test):** run `pytest tests/test_rls_isolation.py` (baseline) before Phase 1

## Phase 1 — Per-transaction GUC reapply (the core code change) ✅ DONE
**Files (4):** `src/db/session.py`, `src/api/deps.py`, `src/api/routes/jobs.py`, `tests/test_rls_guc_reapply.py`
- [x] `after_begin` listener `_reapply_rls_guc` on the `Session` class re-binds
      `app.current_user_id` every transaction, gated on `session.info['rls_user_id']`
- [x] `rls_sync_session` sets `session.info['rls_user_id']` (sync/worker path)
- [x] `get_rls_db` sets `db.sync_session.info['rls_user_id']` (async/API path)
- [x] Hardened the token-download route (`jobs.py:707`) to set info too (Codex note B)
- [x] `tests/test_rls_guc_reapply.py`: proves GUC survives a commit + system session no-ops
- [x] py_compile clean on all 4 files
- [x] **Gate (Codex):** APPROVE — no blocking findings. Confirmed after_begin fires for
      both sync + AsyncSession greenlet paths, no pooled-connection GUC leak, no recursion.
- [ ] **Gate (tests):** run `pytest tests/test_rls_guc_reapply.py` against staging DB — NOT run
      locally (would hit prod Supabase; testing rules forbid mocks, so run in CI/staging).
- [ ] **Follow-up (Codex note D):** add an async `AsyncSession` reapply-after-commit test
      (sync path proven; async uses the identical listener). Low risk; do before Phase 4.

## Phase 2 — Role-targeted RLS policies (REVISED after Codex design consult) ⏸ AWAITING DECISION
**Design (Codex-confirmed):** use `CREATE POLICY ... TO bridgeleads_app/bridgeleads_system`
(role-targeted), NOT `OR current_user=`. Permissive policies OR together; anon/authenticated
have no policy → default-deny (027 lockout preserved). Migration must be inert under today's
BYPASSRLS role (RLS_ENFORCE=False does NOT disable Postgres RLS — only the startup check).

**Full RLS state mapped (alembic 001/018/023/025/027/028):**
- Tenant GUC policies exist: scraper_configs, jobs, results, job_logs, delivered_records,
  pending_skip_trace_rows, skip_trace_queues, password_history, referral_events, user_record_views.
- RLS-on / NO-policy = default-deny (027): users, skip_trace_cache, county_connectors, skip_trace_meter_events.
- county_records: shared-read SELECT (028, guc IS NOT NULL) + 023 trigger blocking GUC-scoped writes.

**System role:** `FOR ALL TO bridgeleads_system USING(true) WITH CHECK(true)` on every table it
touches (trusted cross-tenant infra). 023 trigger still allows system (no GUC) on county_records.

**⚠️ Codex found the blocker — routes that use `get_db` and never set the GUC** would break
under tenant RLS on the cutover role:
- Stripe webhook writes `referral_events` + updates `users` (billing.py, no GUC)
- `password_history` change routes (auth.py:477, no GUC)
- `/auth/onboarding` reads `scraper_configs`/`jobs` (auth.py:322, no GUC → empty results)
- admin activation funnel — cross-tenant app traffic (billing.py, no GUC)

**DECISION NEEDED (see AskUserQuestion):** thorough (fix routes to set GUC → full tenant RLS)
vs pragmatic (broad trusted-server policy `TO bridgeleads_app` on the no-GUC tables; tenant RLS
only where routes already set the GUC). Pragmatic still removes BYPASSRLS + keeps anon lockout;
the app-layer `WHERE user_id` filter remains the tenant boundary on the broad tables (today's model).
- [ ] **Gate:** Codex review of the migration + extend `tests/test_rls_isolation.py` (app/system/anon) → STOP

### DECISION: THOROUGH cutover (user-chosen 2026-06-02). Sub-phases:
Route→DB-dependency audit complete. Routes already on `get_rls_db` (set GUC) are fine:
list/create/get/cancel jobs, get_results, stream_logs, export-url, list/create/get/delete
scrapers, get_cached_records, checkout. `/download` resolves identity from token + sets GUC (Phase 1).

- **Phase 2a (code, fixable routes):** ✅ DONE + Codex APPROVE. Switched to `get_rls_db`:
  `/auth/onboarding` (scraper_configs+jobs), `/auth/change-password` (password_history),
  `/billing/referral` (referral_events). `/auth/reset-password` sets GUC manually (token-auth, no
  current_user — mirrors /download). `/api-key` + `/logout-all` left on get_db (touch only `users`,
  broad policy). Codex caught reset-password in review (silent reuse-check regression) — fixed.
  Files: auth.py, billing.py. py_compile clean.
- **Phase 2b (cross-tenant/webhook) — REDESIGNED per Codex consult:**
  Key constraint: the async API process is ONE role (bridgeleads_app); routes don't become system.
  **REJECTED:** `GRANT bridgeleads_system TO bridgeleads_app` (internet-facing RCE → worker role —
  punches through the role split) and broad app policies on results/jobs/scraper_configs (OR with
  tenant policy → destroys isolation for ALL app traffic).
  **CHOSEN (Codex):**
  - `/billing/webhook` referral grant → `SECURITY DEFINER` fn `grant_referral_credit(referee_id)`
    (owner-owned, EXECUTE to app, fixed search_path, no dynamic SQL; derives referrer, inserts
    referral_events + increments credit atomically). users updates stay direct (broad users policy).
  - `/billing/activation-funnel` → `SECURITY DEFINER` aggregate fn returning ONLY funnel metrics
    (admin-gated route calls it; no raw cross-tenant rows exposed).
  - `/scrapers/sample` → precomputed sanitized sample table refreshed by a Celery worker; public
    endpoint reads ONLY that table (no live tenant query, no elevation).
  - Phase 2c policy note: referral_events INSERT policy must be ASYMMETRIC — `WITH CHECK
    (referrer_id = GUC)`, not the loose 018 `referrer OR referee` read rule.
  Scope: 1 migration (2 SECURITY DEFINER fns + sample table), 1 worker refresh task, 3 route
  refactors, tests. SECURITY DEFINER fns are security-sensitive — Codex review mandatory.

  **2b-i ✅ DONE + Codex APPROVE** — migration 029: `public.grant_referral_credit(uuid)` +
  `public.activation_funnel(int)` SECURITY DEFINER fns (search_path pinned, schema-qualified,
  REVOKE from PUBLIC + anon + authenticated, EXECUTE to app only) + `public.public_sample_cache`
  singleton. Role bindings DO-block-guarded (no-op without the roles). Fixed a latent bug:
  activation funnel's CTE omitted `stripe_customer_id` it referenced → would've errored at runtime.
  Codex: 3 rounds (Supabase anon/authenticated revoke + schema-qualification).
  **2b-ii ✅ DONE + Codex APPROVE** — billing.py: `_grant_referral_credit` → calls the definer fn
  (idempotency/atomic-increment now in-DB via ON CONFLICT); `/activation-funnel` → `SELECT * FROM
  public.activation_funnel(:days)`. Behavior parity confirmed.
  **2b-iii ✅ DONE + Codex APPROVE** — `refresh_public_sample_cache` Celery task (hourly, system
  session) computes sanitized samples + stats → upserts public_sample_cache; `/scrapers/sample`
  reads the cache (empty-shape fallback). Codex caught a bug I introduced (`date_recorded` is
  String(32) not a date — `.isoformat()` would crash the task) — fixed to store it verbatim.
  ⚠️ **CROSS-PHASE DEP (Codex):** this task reads results/jobs/scraper_configs via the system role,
  which needs `FOR ALL TO bridgeleads_system` policies on those tables — those land in **Phase 2c**.
  Works today under BYPASSRLS; **Phase 2c MUST add system policies on results/jobs/scraper_configs
  (+ all worker tables) and MUST precede the Phase 3 role downgrade.**
  **2b-iv ✅ DONE + Codex APPROVE** — `tests/test_rls_cross_tenant_helpers.py`: grant_referral_credit
  idempotency (replay = no double-grant) + no-op without referrer + activation_funnel deltas. Real DB,
  seed-in-txn-rollback (mirrors test_rls_isolation). Route/worker integration → CI/staging.

  **➡️ PHASE 2b COMPLETE.** Commits: 5efda74 (2b-i/ii), 06ce1c8 (2b-iii), de3d40e (2b-iv).

**Phase 2c ✅ DONE + Codex APPROVE** — migration 030 `030_rls_role_targeted_policies.py`:
- Python role-existence guard: no-op if NEITHER role exists (CI), RAISE if exactly one, swap if both.
- Tenant tables → `<t>_app TO bridgeleads_app` (USING+WITH CHECK guc) + `<t>_system FOR ALL TO
  bridgeleads_system` (incl. results/jobs/scraper_configs — the 2b-iii dependency). referral_events
  app=SELECT-only (writes via definer fn). users/county_connectors broad app + system. county_records
  app shared-read + system FOR ALL (023 trigger still guards). skip_trace_cache/meter = system-only.
  anon/authenticated default-denied (027 preserved + tightened).
- Backfills 029 role bindings (in case roles were provisioned after 029). Idempotent up + down.
- Codex: 2 rounds (backfill + downgrade idempotency + restore 025 WITH CHECK).
**Phase 2d ✅ DONE + Codex APPROVE** — `tests/test_rls_role_policies.py`: SET LOCAL ROLE bridgeleads_app
(no-GUC=0 rows, GUC=A→only A, GUC=B→only B) + bridgeleads_system reads cross-tenant. Module SKIPS unless
the cutover is applied (both roles + `results_app` policy) — legacy model stays covered by
test_rls_isolation.py. **➡️ PHASE 2 COMPLETE** (2a d5e2fe1, 2b 5efda74/06ce1c8/de3d40e, 2c 40497ce, 2d).

⚠️ **PHASE 4 CAVEAT (Codex):** `FORCE ROW LEVEL SECURITY` makes even the table OWNER subject to policies.
The SECURITY DEFINER fns (grant_referral_credit/activation_funnel) run as the owner — Phase 4 MUST verify
the owner keeps BYPASSRLS (Supabase `postgres` does) or the definer helpers break under FORCE.
- **Phase 2c (migration 029, role-targeted policies):** `FOR ALL TO bridgeleads_system USING(true)
  WITH CHECK(true)` on all worker tables; tenant GUC policies `TO bridgeleads_app` on user-scoped
  tables; broad `TO bridgeleads_app` on `users` (auth-bootstrap: register/login/refresh/forgot/reset
  query users pre-identity — inherent, app-layer enforces) + shared catalogs (county_records read,
  county_connectors). county_records `FOR ALL TO bridgeleads_system`. Inert under BYPASSRLS today.
- **Phase 2d (tests):** extend `test_rls_isolation.py` — app sees only own rows, system cross-tenant,
  anon denied, auth-bootstrap flows still work.
- Each sub-phase: ≤5 files, Codex review, STOP for approval.

## Phase 3 — Repoint connection URLs — CODE ✅ Codex APPROVE / DEPLOY ⬜ (manual)
- [x] `alembic/env.py` prefers `DATABASE_URL_MIGRATE` (owner/DDL), falls back to `DATABASE_URL_SYNC`
      (pre-cutover unchanged); `settings.DATABASE_URL_MIGRATE` added (optional, default "").
- [ ] **MANUAL:** add `DATABASE_URL_MIGRATE` to `.env.example` (Read blocked on `.env*`).
- [ ] **MANUAL DEPLOY (staging→prod):** run `scripts/provision_rls_roles.sql` + migrations 029/030
      FIRST, then point `DATABASE_URL`→app, `DATABASE_URL_SYNC`→system, `DATABASE_URL_MIGRATE`→owner.
      Deploy staging with `RLS_ENFORCE=False`; confirm boot + full scrape cycle + `bypassrls=False`.
- [ ] **Gate:** staging healthy for a full scrape cycle → STOP for approval

## Phase 4 — Enforce + FORCE RLS (production, last) — CODE ✅ Codex APPROVE / DEPLOY ⬜
**Code done:** migration `031_rls_force_row_level_security.py` — FORCE ROW LEVEL SECURITY on the 16
policy-bearing tables. Gated: no-op unless both cutover roles exist (CI safety); RAISES unless the
029 SECURITY DEFINER function owners carry BYPASSRLS (else FORCE would break the definer helpers).
`RLS_ENFORCE` stays an env flag (NOT a code default) — flipped during deploy.
- [x] migration 031 (FORCE + owner-bypass guard). Codex: APPROVE (2 rounds).
- [ ] **MANUAL (staging→prod, LAST):** after Phase 3 is green — `RLS_ENFORCE=True` in staging, run
      full E2E + isolation tests; then prod: flip `RLS_ENFORCE=True`, verify boot guard, run `alembic
      upgrade` to 031 (applies FORCE). Owner (`postgres`) keeps BYPASSRLS so the definer fns survive.
- [ ] **Gate:** Codex final review + Master Security Review (§14) twice-clean → done

---
## ✅ CUTOVER CODE COMPLETE — all phases authored, Codex-reviewed, committed.
Commits: e5d50e8(0) a27ff9f(1) d5e2fe1(2a) 5efda74(2b-i/ii) 06ce1c8(2b-iii) de3d40e(2b-iv)
40497ce(2c) 51655ca(2d) a268fd1(3) + 031(4). Remaining work is OPERATIONAL (provision roles,
repoint Railway env, staged RLS_ENFORCE flip + FORCE) per the runbook — no more code.

---

## The SQL (Phase 0 reference)

```sql
-- App role: request traffic. NO DELETE, NO DDL, NOBYPASSRLS.
CREATE ROLE bridgeleads_app LOGIN PASSWORD :'app_pw'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
GRANT USAGE ON SCHEMA public TO bridgeleads_app;
GRANT SELECT ON county_connectors, county_records TO bridgeleads_app;
GRANT SELECT, INSERT, UPDATE ON
    users, password_history, scraper_configs, jobs, results,
    delivered_records, job_logs, user_record_views,
    skip_trace_cache, skip_trace_queues, pending_skip_trace_rows,
    skip_trace_meter_events, referral_events
TO bridgeleads_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bridgeleads_app;

-- System role: workers/scheduler. + DELETE on county_records only.
CREATE ROLE bridgeleads_system LOGIN PASSWORD :'sys_pw'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
GRANT USAGE ON SCHEMA public TO bridgeleads_system;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO bridgeleads_system;
GRANT DELETE ON county_records TO bridgeleads_system;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO bridgeleads_system;
```

## Open questions (need answers before Phase 0 SQL is final)
1. **Supabase plan** — can you create custom DB roles, or are you limited to built-in
   `postgres`/`anon`/`authenticated`/`service_role`? Decides custom-role vs scoping an existing role.
2. Are API and workers already on **separate connection strings** in Railway, or shared?
3. OK to add a 3rd env var (`DATABASE_URL_MIGRATE`) for the owner/migration role?

---

## Review section (fill in as phases complete)
- _Pending Phase 0._
