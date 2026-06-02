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

## Phase 2 — System-role RLS carve-out for cross-tenant work
**Files (≤5):** `alembic/versions/0XX_system_role_rls_policies.py`, `tests/test_rls_isolation.py` (extend)
- [ ] `system_sync_session()` sets no GUC (canary, watchdog, ingest, retention). Under
      NOBYPASSRLS it would be blocked by user-scoped policies.
- [ ] Add policies per user-scoped table: `USING (user_id = <guc> OR current_user = 'bridgeleads_system')`
      + matching `WITH CHECK`. Named-role carve-out = explicit, greppable, not a settable GUC.
- [ ] Extend `test_rls_isolation.py`: (a) `bridgeleads_app` sees only its rows, (b) `bridgeleads_system`
      can do cross-tenant ops, (c) anon still denied.
- [ ] **Gate:** isolation tests green + Codex review → STOP for approval

## Phase 3 — Repoint connection URLs to the downgraded roles
**Files (≤5):** `.env.example`, Railway env (manual), `src/db/session.py` (migrate URL if needed), runbook
- [ ] `DATABASE_URL` → `bridgeleads_app`; `DATABASE_URL_SYNC` → `bridgeleads_system`;
      add `DATABASE_URL_MIGRATE` → `bridgeleads_owner` (alembic only)
- [ ] Update `.env.example` + document; rotate any exposed credentials during the swap
- [ ] Deploy to **staging** with `RLS_ENFORCE=False` (advisory) — confirm boot, scrapes run,
      startup role-status log shows `bypassrls=False`
- [ ] **Gate:** staging healthy for a full scrape cycle → STOP for approval

## Phase 4 — Enforce + FORCE RLS (production, last)
**Files (≤5):** `settings.py` (flip default), `alembic/versions/0XX_force_rls.py`, runbook
- [ ] Staging: `RLS_ENFORCE=True`, run full E2E (scrape→enrich→export→deliver) + isolation tests
- [ ] Production: deploy roles → flip `RLS_ENFORCE=True` → verify boot guard passes
- [ ] **LAST:** `ALTER TABLE ... FORCE ROW LEVEL SECURITY` on user-scoped tables — only after
      everything above is green in prod
- [ ] **Gate:** Codex final review + Master Security Review (§14) twice-clean → done

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
