# HIGH-2 — Restore real RLS enforcement (drop the BYPASSRLS role)

**Status:** PLAN — not yet implemented. Requires staging DB + new role provisioning, and sign-off.
**Source:** `docs/security/REVIEW-2026-06-01.md` HIGH-2. Read-only audit 2026-06-01 (2 agents) + Codex design review (session `019e8443`).

## Problem
Production runs as a `BYPASSRLS` (likely superuser-ish, table-owning) role, so the 10 RLS
policies are **decorative** — the only tenant boundary is the app-layer `WHERE user_id` filter.
A single missed filter = cross-tenant leak with no DB backstop.

Two facts make this non-trivial:
- **No table uses `FORCE ROW LEVEL SECURITY`.** If the runtime role owns the tables, dropping
  `BYPASSRLS` alone is a **no-op** — non-FORCE RLS never applies to the table owner.
- **Worker/system tasks legitimately span tenants** (watchdog, scheduler dispatch, skip-trace
  ingest, the `run_scrape_job` bootstrap that reads `jobs` to learn `user_id`). Under enforced
  RLS with no context these **fail closed (0 rows)** — silently breaking scraping/billing.

## RLS tables (10) — all USING-only, NULLIF-wrapped (fail-closed when unset)
`scraper_configs`, `jobs`, `results`, `job_logs` (via `job_id`→`jobs.user_id`), `delivered_records`,
`pending_skip_trace_rows`, `skip_trace_queues`, `password_history`, `referral_events`, `user_record_views`.
**`users`** has NO RLS (tenant root — cross-tenant scheduler tasks rely on this).
**`county_records`** RLS-disabled; a BEFORE-INSERT trigger BLOCKS writes when `app.current_user_id`
IS set → system paths (unset) are allowed; a user-context session must NOT write it.

## Call sites that break under enforced RLS (must be fixed first)
System (`system_sync_session`, no context today):
- `tasks.py:146` `run_scrape_job` bootstrap — reads `jobs` to get `user_id` (chicken/egg) **[CRITICAL — blocks all scrapes]**
- `scheduler.py:205` `watchdog_stuck_jobs` — cross-tenant `jobs` read+update
- `skip_trace_dispatcher.py:60` — `pending_skip_trace_rows` + `skip_trace_queues`
- `tracerfy_ingest.py:96` — `results` + `pending_skip_trace_rows` + `skip_trace_queues`
- `tasks.py:87` `_publish_log` fallback — `job_logs` INSERT
Raw `SyncSessionLocal()` (no context, must route through a system session):
- `scheduler.py:86` `dispatch_scheduled_jobs` — `scraper_configs`/`users`/`jobs` **[enqueues nothing if broken]**
- `scheduler.py:541` `send_onboarding_emails` — `scraper_configs`/`jobs`
- `tasks.py:1154` `_resolve_date_range` — `jobs` (currently swallowed → silently degrades to 30-day window)
- `scheduler.py:430` `expire_trials` — `users` only (safe today; would break only if `users` gets RLS)

## Design (revised per Codex review)

### Roles — role-based bypass, NOT a GUC bypass
- **`bridgeleads_owner`** (migration/DDL only) — owns the tables. Not used at runtime.
- **`bridgeleads_app`** — `NOBYPASSRLS NOSUPERUSER`, DML only. Used by the FastAPI async engine
  and the worker's per-user `rls_sync_session`.
- **`bridgeleads_system`** — `NOBYPASSRLS NOSUPERUSER`, DML only. Used by `system_sync_session`
  for the legitimately cross-tenant tasks above.
Runtime roles do **not** own tables → `FORCE` actually enforces against them.

### Policies — per table, explicit USING + WITH CHECK + role escape
```sql
-- example for a user_id table
ALTER POLICY jobs_user_isolation ON jobs
  USING (user_id = NULLIF(current_setting('app.current_user_id', true),'')::uuid
         OR current_user = 'bridgeleads_system')
  WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true),'')::uuid
         OR current_user = 'bridgeleads_system');
```
- `job_logs` WITH CHECK validates via `job_id IN (SELECT id FROM jobs WHERE user_id = ...)` OR system role.
- `referral_events` keeps its `referrer_id OR referee_id` two-column check + the system-role term.
- Bypass is **role-based** (`current_user = 'bridgeleads_system'`) — no `app.is_system` GUC (a GUC
  any raw-SQL path could flip is a weak boundary).

### Session-context survival (the bug FORCE would expose)
`SET LOCAL`/`set_config(..., true)` is **transaction-local** — it dies on `commit()`, and the
workers commit inside `rls_sync_session()` blocks. Fix BEFORE the role swap:
- Bind `user_id` to the session and **re-apply `app.current_user_id` on every transaction begin**
  via a SQLAlchemy `after_begin` event listener (standard RLS-with-SQLAlchemy pattern). Apply to
  the app-role engine used by `rls_sync_session`.
- `system_sync_session` needs no GUC — the `bridgeleads_system` role itself is the escape, and
  role is connection-persistent across commits.
- Two sync engines: `sync_engine_app` (bridgeleads_app) + `sync_engine_system` (bridgeleads_system).
  `rls_sync_session` → app engine; `system_sync_session` → system engine. (Avoids `SET ROLE`,
  which is also transaction-local.)

### Code refactors (land with the migration, behind the still-bypassing role so they're no-ops until cutover)
- Route `scheduler.py:86`, `scheduler.py:541`, `tasks.py:1154` through `system_sync_session()`.
- `run_scrape_job`: `system_sync_session` to read `job.user_id`, then `rls_sync_session(user_id)` for the work (already this shape — just needs the system engine).
- Ensure `_publish_log` is always passed the RLS-bound `db` in the worker path (avoid the `job_logs` fallback INSERT), or let it use the system session.
- Add the `after_begin` GUC-reapply listener to the app engine.

## Migration sequence (forward-only; STAGING first)
**Staging:** 1) provision `bridgeleads_owner`/`app`/`system` roles + grants (DML to app/system, ownership to owner). 2) Alembic: rewrite policies (USING+WITH CHECK+system-role escape). 3) Deploy code (engines, `after_begin` listener, system-session routing) — still on old creds (no-op). 4) Switch staging env URLs to the new roles. 5) Run the extended RLS test suite (below). 6) `ALTER TABLE ... FORCE ROW LEVEL SECURITY` on all 10 — **last**. 7) Burn-in: a full scrape, watchdog re-queue, skip-trace ingest, scheduler dispatch, onboarding email, Stripe webhook.
**Prod:** policy migration → deploy code → switch creds → enable FORCE last.
**Rollback:** fast = switch env back to the old BYPASSRLS creds; DB = `ALTER TABLE ... NO FORCE`; leave the (backward-compatible) policy clauses in place.

## Verification (extend `tests/test_rls_isolation.py`)
Currently covers only `results` + `delivered_records` under a granted non-owner role. Extend to:
- All 10 RLS tables: unset context → 0 rows; user A → only A; user B → only B.
- **WITH CHECK**: a user INSERT/UPDATE setting another user's `user_id` is rejected.
- **System role**: `bridgeleads_system` sees/writes cross-tenant (the escape works).
- **FORCE/owner case**: run under the actual `bridgeleads_app` role (not just a granted test role) so the owner-bypass gap is exercised.
- **Mid-commit survival**: a session that commits then issues another query still has RLS context (proves the `after_begin` listener).

## Out of scope here (decide separately)
- Whether to add RLS to `users` (tenant root; currently filter-only). High value but more cross-tenant scheduler refactoring.
