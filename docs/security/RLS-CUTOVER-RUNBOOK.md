# RLS Least-Privilege Cutover — Runbook

**Created:** 2026-06-02 · **Branch:** `security/redteam-remediation-2026-06-01`
**Plan:** `tasks/rls-cutover-todo.md` · **Phase 0 SQL:** `scripts/provision_rls_roles.sql`

## Why this exists

A SQL-injection audit (Claude × Codex, 2026-06-02) found **no SQLi** in the app —
every query is parameterized. The real "an attacker/bug could wipe a table" risk is
that production connects to Postgres as a **single role with `BYPASSRLS`** (see
`settings.py:101-108`, `src/db/session.py` `check_rls_role_status`). With one bug in a
`WHERE user_id = ...` filter, that role can read or destroy across every tenant, and the
RLS policies in the migrations are decorative.

This runbook downgrades to three scoped roles and makes RLS a real enforcement boundary.

## The danger (read before touching anything)

`RLS_ENFORCE=True` or a `NOBYPASSRLS` connection role, applied **out of order**, will:
- block the workers' cross-tenant operations (canary, watchdog, ingest, retention), and
- break per-tenant queries after the mid-task `commit()` in `run_scrape_job` (the
  `app.current_user_id` GUC is transaction-local and is cleared by the commit), and
- trip the startup guard in `check_rls_role_status()` → the app refuses to boot.

So the phases below MUST land in order. `FORCE ROW LEVEL SECURITY` is dead last.

## Roles

| Role | Connection var (Phase 3) | Privileges | RLS |
|---|---|---|---|
| existing owner | `DATABASE_URL_MIGRATE` (alembic only) | DDL | n/a |
| `bridgeleads_app` | `DATABASE_URL` (FastAPI) | SELECT/INSERT/UPDATE app tables; **no DELETE/DDL** | NOBYPASSRLS |
| `bridgeleads_system` | `DATABASE_URL_SYNC` (workers, scheduler) | SELECT/INSERT/UPDATE all + DELETE `county_records` | NOBYPASSRLS + named-role policy |

Note: alembic currently reads `DATABASE_URL_SYNC` (`alembic/env.py:15`), the same var
the workers use. Phase 3 adds `DATABASE_URL_MIGRATE` and makes `alembic/env.py` prefer it
(fallback to `DATABASE_URL_SYNC` so nothing breaks pre-cutover).

## Phases (each is a STOP-for-approval gate)

### Phase 0 — provision roles (DONE, no prod impact)
Run once as superuser/owner. Generates passwords at runtime; nothing secret is committed:
```bash
psql "$ADMIN_DATABASE_URL" \
  -v app_pw="$(openssl rand -base64 32)" \
  -v sys_pw="$(openssl rand -base64 32)" \
  -f scripts/provision_rls_roles.sql
```
Verify:
```sql
SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname LIKE 'bridgeleads_%';
-- both rows: rolsuper=f, rolbypassrls=f
SELECT grantee, table_name FROM information_schema.role_table_grants
WHERE grantee='bridgeleads_app' AND privilege_type='DELETE';
-- expect ZERO rows
```
Save the two passwords for Phase 3 env vars. **This script has no runtime effect until
Phase 3 repoints the connection strings.**

### Phase 1 — per-transaction GUC reapply (code)
Make `app.current_user_id` survive the mid-task commit (SQLAlchemy `after_begin` event
listener keyed off the session's user_id), on both async and sync RLS sessions. New test:
`tests/test_rls_guc_reapply.py`. Without this, Phase 4 breaks `run_scrape_job`.

### Phase 2 — system-role policy carve-out (migration)
Add to each user-scoped table:
`USING (user_id = NULLIF(current_setting('app.current_user_id', true),'')::uuid
        OR current_user = 'bridgeleads_system')` plus matching `WITH CHECK`. This lets
the NOBYPASSRLS system role do legitimate cross-tenant work without a global bypass.
Extend `tests/test_rls_isolation.py` for app/system/anon.

### Phase 3 — repoint connections (staging first)
Set the three connection vars to the new roles. Deploy to **staging** with
`RLS_ENFORCE=False` (advisory). Confirm boot, a full scrape cycle, and the startup
role-status log shows `bypassrls=False`.

### Phase 4 — enforce, then FORCE (production, last)
Staging `RLS_ENFORCE=True` → full E2E + isolation tests. Then production: deploy roles →
`RLS_ENFORCE=True` → verify boot guard passes → **last:** `ALTER TABLE ... FORCE ROW
LEVEL SECURITY` on user-scoped tables.

## Rollback

Each phase is independently revertible:
- Phase 0: `DROP ROLE bridgeleads_app, bridgeleads_system;` (only if unused by any connection).
- Phase 1/2: revert the commits; migrations have downgrades.
- Phase 3: repoint the connection vars back to the original role.
- Phase 4: `RLS_ENFORCE=False` and `ALTER TABLE ... NO FORCE ROW LEVEL SECURITY`.

The fastest full rollback at any point: set the connection vars back to the original
BYPASSRLS role and `RLS_ENFORCE=False` — returns to today's behavior immediately.
